"""
Kite Connect API wrapper with TOTP auto-login and simulation mode.
Handles token management, authentication, and market data fetching.
"""

import os
import json
import logging
from datetime import datetime, date
from enum import Enum

import models

logger = logging.getLogger(__name__)


class Permission(Enum):
    READONLY = "READONLY"
    EXECUTE = "EXECUTE"


# Simulation mode stock data (used when Kite credentials are not configured)
SIMULATION_STOCKS = {
    "RELIANCE": {"ltp": 2520, "lot_size": 250, "haircut": 0.125, "iv": 0.22, "exchange": "NFO"},
    "TCS": {"ltp": 3050, "lot_size": 175, "haircut": 0.125, "iv": 0.26, "exchange": "NFO"},
    "HDFCBANK": {"ltp": 1620, "lot_size": 550, "haircut": 0.125, "iv": 0.18, "exchange": "NFO"},
    "INFY": {"ltp": 1390, "lot_size": 400, "haircut": 0.125, "iv": 0.28, "exchange": "NFO"},
    "BEL": {"ltp": 338, "lot_size": 1500, "haircut": 0.18, "iv": 0.35, "exchange": "NFO"},
    "SBIN": {"ltp": 755, "lot_size": 1500, "haircut": 0.15, "iv": 0.24, "exchange": "NFO"},
    "HAL": {"ltp": 4150, "lot_size": 150, "haircut": 0.20, "iv": 0.38, "exchange": "NFO"},
    "ICICIBANK": {"ltp": 1095, "lot_size": 700, "haircut": 0.125, "iv": 0.19, "exchange": "NFO"},
    "NIFTY": {"ltp": 23150, "lot_size": 25, "iv": 0.145, "exchange": "NFO",
              "support": 22800, "resistance": 23500},
    "BANKNIFTY": {"ltp": 48900, "lot_size": 15, "iv": 0.162, "exchange": "NFO",
                  "support": 47800, "resistance": 50000},
}


class KiteService:
    def __init__(self):
        self.api_key = os.getenv("KITE_API_KEY", "")
        self.api_secret = os.getenv("KITE_API_SECRET", "")
        self.user_id = os.getenv("KITE_USER_ID", "") or models.get_setting("kite_user_id", "")
        self.totp_secret = os.getenv("KITE_TOTP_SECRET", "") or models.get_setting("kite_totp_secret", "")
        self.access_token = None
        self.kite = None
        self.permission = Permission.READONLY
        self._password = None  # NEVER persisted - session only
        self._holdings = []
        self._cash_balance = 0.0

        # Check for stored token from today
        stored_token = models.get_setting("kite_access_token")
        stored_date = models.get_setting("kite_token_date")
        if stored_token and stored_date == str(date.today()):
            self.access_token = stored_token
            self._init_kite_with_token()

    def _init_kite_with_token(self):
        """Initialize Kite client with existing token."""
        if not self.api_key:
            return
        try:
            from kiteconnect import KiteConnect
            self.kite = KiteConnect(api_key=self.api_key)
            self.kite.set_access_token(self.access_token)
        except ImportError:
            logger.warning("kiteconnect package not installed, running in simulation mode")

    @property
    def is_simulation(self):
        return not self.api_key or not self.access_token

    def is_authenticated(self):
        return self.access_token is not None and self.kite is not None

    def get_login_url(self):
        if not self.api_key:
            return None
        try:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=self.api_key)
            return kite.login_url()
        except ImportError:
            return None

    def handle_callback(self, request_token):
        """Handle OAuth callback from Kite."""
        try:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=self.api_key)
            data = kite.generate_session(request_token, api_secret=self.api_secret)
            self.access_token = data["access_token"]
            self.kite = kite
            self.kite.set_access_token(self.access_token)

            # Store token for today
            models.set_setting("kite_access_token", self.access_token)
            models.set_setting("kite_token_date", str(date.today()))

            models.create_notification("AUTO_LOGIN_SUCCESS", "Kite Login Successful",
                                      "Connected to Kite API via OAuth callback", "INFO")
            return True
        except Exception as e:
            logger.error(f"Kite callback failed: {e}")
            models.create_notification("AUTO_LOGIN_FAILED", "Kite Login Failed",
                                      f"OAuth callback error: {str(e)}", "URGENT")
            return False

    def auto_login(self):
        """
        Automated Kite login using TOTP (no browser needed).
        Uses pyotp to generate TOTP code, then POSTs to Kite login endpoints.
        """
        if not all([self.api_key, self.api_secret, self.user_id, self.totp_secret]):
            return {"success": False, "error": "Missing Kite credentials for auto-login"}

        password = self._password or os.getenv("KITE_PASSWORD", "")
        if not password:
            return {"success": False, "error": "No password available for auto-login"}

        try:
            import pyotp
            import requests
            from urllib.parse import urlparse, parse_qs
            from kiteconnect import KiteConnect

            kite = KiteConnect(api_key=self.api_key)
            session = requests.Session()

            # Step 1: Login with user_id + password
            login_resp = session.post(
                "https://kite.zerodha.com/api/login",
                data={"user_id": self.user_id, "password": password}
            )
            request_id = login_resp.json()["data"]["request_id"]

            # Step 2: 2FA with TOTP
            totp = pyotp.TOTP(self.totp_secret).now()
            session.post(
                "https://kite.zerodha.com/api/twofa",
                data={"user_id": self.user_id, "request_id": request_id, "twofa_value": totp}
            )

            # Step 3: Get request_token from redirect
            redirect_url = kite.login_url()
            resp = session.get(redirect_url + "&skip_session=true", allow_redirects=False)
            location = resp.headers.get("Location", "")
            request_token = parse_qs(urlparse(location).query).get("request_token", [None])[0]

            if not request_token:
                raise Exception("No request_token in redirect")

            # Step 4: Generate session
            data = kite.generate_session(request_token, api_secret=self.api_secret)
            self.access_token = data["access_token"]
            self.kite = kite
            self.kite.set_access_token(self.access_token)

            # Store token
            models.set_setting("kite_access_token", self.access_token)
            models.set_setting("kite_token_date", str(date.today()))

            # Discard password from memory
            self._password = None

            models.create_notification("AUTO_LOGIN_SUCCESS", "TOTP Auto-Login Successful",
                                      f"Kite connected for user {self.user_id}", "INFO")
            return {"success": True}

        except Exception as e:
            logger.error(f"Auto-login failed: {e}")
            self._password = None  # Always discard
            models.create_notification("AUTO_LOGIN_FAILED", "TOTP Auto-Login Failed",
                                      f"Error: {str(e)}", "URGENT")
            return {"success": False, "error": str(e)}

    def set_permission(self, level, confirm=False, understand_risk=False):
        """Change permission level with safety checks."""
        if level == "EXECUTE":
            if not confirm or not understand_risk:
                return {"success": False, "error": "Must confirm and acknowledge risk"}
            self.permission = Permission.EXECUTE
            return {"success": True, "permission": "EXECUTE"}
        else:
            self.permission = Permission.READONLY
            return {"success": True, "permission": "READONLY"}

    def get_permission(self):
        return self.permission.value

    def lock_execution(self):
        """Lock execution back to READONLY (e.g., after reconciliation mismatch)."""
        self.permission = Permission.READONLY
        models.create_notification("RECONCILIATION_MISMATCH",
                                  "Execution LOCKED",
                                  "Order mismatch detected. Execution locked to READONLY.",
                                  "URGENT")

    # --- Market Data ---

    def get_holdings(self):
        """Fetch holdings from Kite or return simulation data."""
        if self._holdings:
            return self._holdings
        if self.is_simulation:
            return self._get_simulation_holdings()
        try:
            return self.kite.holdings()
        except Exception as e:
            logger.error(f"Failed to fetch holdings: {e}")
            models.create_notification("TOKEN_EXPIRED", "Kite Data Error",
                                      f"Failed to fetch holdings: {e}", "URGENT")
            return []

    def set_holdings(self, holdings, cash_balance=0):
        """Set holdings from import (CSV, manual, etc.)."""
        self._holdings = holdings
        self._cash_balance = cash_balance

    def get_ltp(self, symbol, exchange="NFO"):
        """Get last traded price."""
        if self.is_simulation:
            stock = SIMULATION_STOCKS.get(symbol)
            return stock["ltp"] if stock else None
        try:
            data = self.kite.ltp(f"{exchange}:{symbol}")
            return data[f"{exchange}:{symbol}"]["last_price"]
        except Exception as e:
            logger.error(f"Failed to fetch LTP for {symbol}: {e}")
            return None

    def get_option_chain(self, symbol, expiry_date=None):
        """Get option chain for a symbol. Returns simulated data if not connected."""
        if self.is_simulation:
            return self._simulate_option_chain(symbol)
        try:
            instruments = self.kite.instruments(exchange="NFO")
            chain = [i for i in instruments if i["name"] == symbol
                     and i["instrument_type"] in ("CE", "PE")]
            if expiry_date:
                chain = [i for i in chain if str(i["expiry"]) == expiry_date]
            return chain
        except Exception as e:
            logger.error(f"Failed to fetch option chain: {e}")
            return []

    def get_lot_size(self, symbol):
        """Get lot size for a symbol."""
        stock = SIMULATION_STOCKS.get(symbol)
        if stock:
            return stock["lot_size"]
        return 1

    def place_order(self, order_params):
        """Place order on Kite. Requires EXECUTE permission."""
        if self.permission != Permission.EXECUTE:
            return {"success": False, "error": "Permission denied: READONLY mode"}
        if self.is_simulation:
            return {"success": True, "order_id": f"SIM-{models.generate_id()[:8]}",
                    "simulated": True}
        try:
            order_id = self.kite.place_order(
                variety="regular",
                exchange=order_params.get("exchange", "NFO"),
                tradingsymbol=order_params["tradingsymbol"],
                transaction_type=order_params["action"],
                quantity=order_params["qty"],
                product=order_params.get("product", "NRML"),
                order_type=order_params.get("order_type", "LIMIT"),
                price=order_params.get("price"),
            )
            return {"success": True, "order_id": order_id}
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return {"success": False, "error": str(e)}

    def place_gtt(self, params):
        """Place GTT stop-loss order on Kite."""
        if self.is_simulation:
            return {"success": True, "gtt_id": f"GTT-SIM-{models.generate_id()[:8]}",
                    "simulated": True}
        try:
            gtt_id = self.kite.place_gtt(
                trigger_type="single",
                tradingsymbol=params["tradingsymbol"],
                exchange=params.get("exchange", "NFO"),
                trigger_values=[params["trigger_price"]],
                last_price=params.get("last_price", params["trigger_price"]),
                orders=[{
                    "transaction_type": "BUY",
                    "quantity": params["quantity"],
                    "order_type": "MARKET",
                    "product": "NRML",
                    "price": 0,
                }],
            )
            return {"success": True, "gtt_id": gtt_id}
        except Exception as e:
            logger.error(f"GTT placement failed: {e}")
            return {"success": False, "error": str(e)}

    def get_orders(self):
        """Fetch order book from Kite."""
        if self.is_simulation:
            return []
        try:
            return self.kite.orders()
        except Exception as e:
            logger.error(f"Failed to fetch orders: {e}")
            return []

    # --- Simulation Helpers ---

    def _get_simulation_holdings(self):
        """Generate sample holdings for simulation mode."""
        holdings = []
        for symbol, data in SIMULATION_STOCKS.items():
            if symbol in ("NIFTY", "BANKNIFTY"):
                continue
            holdings.append({
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "quantity": data["lot_size"],
                "average_price": data["ltp"] * 0.95,
                "last_price": data["ltp"],
                "pnl": data["ltp"] * data["lot_size"] * 0.05,
                "lot_size": data["lot_size"],
                "haircut": data["haircut"],
                "collateral_value": data["ltp"] * data["lot_size"] * (1 - data["haircut"]),
            })
        return holdings

    def _simulate_option_chain(self, symbol):
        """Generate simulated option chain for a symbol."""
        from black_scholes import option_price, delta as calc_delta

        stock = SIMULATION_STOCKS.get(symbol)
        if not stock:
            return []

        spot = stock["ltp"]
        iv = stock["iv"]
        T = 7 / 365  # 1 week to expiry
        chain = []

        # Generate strikes at regular intervals
        if "NIFTY" in symbol:
            strike_step = 50 if symbol == "NIFTY" else 100
        else:
            strike_step = max(10, int(spot * 0.01))

        base_strike = int(spot / strike_step) * strike_step

        for i in range(-10, 11):
            strike = base_strike + i * strike_step
            for opt_type in ("CE", "PE"):
                price = option_price(spot, strike, T, sigma=iv, option_type=opt_type)
                d = calc_delta(spot, strike, T, sigma=iv, option_type=opt_type)
                chain.append({
                    "strike": strike,
                    "option_type": opt_type,
                    "ltp": round(price, 2),
                    "iv": iv,
                    "delta": round(d, 4),
                    "lot_size": stock["lot_size"],
                    "exchange": "NFO",
                    "tradingsymbol": f"{symbol}{strike}{opt_type}",
                })

        return chain


# Singleton
kite_service = KiteService()
