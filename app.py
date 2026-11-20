"""Vercel entrypoint for Edge GEX Lab.

Added for the Vercel deploy only; upstream files are left untouched.
- Vercel's filesystem is read-only except /tmp, so the data/ paths move there.
- Upstream only accepts 127.0.0.1/localhost as Host; this also allows the Vercel domains.
- A login page (login_gate.py) protects the whole app.
- SPX comes from Alpaca options (alpaca_index.py) because Cboe blocks Vercel's servers.
Secrets come from Vercel Environment Variables: APCA_API_KEY_ID, APCA_API_SECRET_KEY,
GEXLAB_LOGIN_USER, GEXLAB_LOGIN_PASSWORD.
"""
import os
from pathlib import Path

_DATA = Path(os.getenv("GEXLAB_DATA_DIR", "/tmp/gexlab-data"))
(_DATA / "cache").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("GEXLAB_DB", str(_DATA / "gexlab.db"))
os.environ.setdefault("GEXLAB_DEMO_DB", str(_DATA / "gexlab_demo.db"))
os.environ.setdefault("GEXLAB_NO_BROWSER", "1")
os.environ.setdefault("GEXLAB_NO_UPDATE_CHECK", "1")

from gexlab import config  # noqa: E402

config.DEMO_DB_DEFAULT = _DATA / "gexlab_demo.db"
config.CACHE_DIR = _DATA / "cache"
config.CALENDAR_DISK_CACHE = _DATA / "cache" / "calendar_alpaca.json"
config.RATE_DB = _DATA / "ratelimit.sqlite"
config.ADV_CACHE_FILE = _DATA / "cache" / "adv_daily.json"
config.ATR_CACHE_FILE = _DATA / "cache" / "atr_daily.json"
# Cboe answers HTTP 403 to Vercel, so open interest for SPY/QQQ/stocks comes from Alpaca too.
config.OI_SOURCE = os.getenv("GEXLAB_OI_SOURCE", "alpaca")

import alpaca_index  # noqa: E402

alpaca_index.install()   # SPX (GEXLAB_ALPACA_INDEX) served from Alpaca options instead of Cboe

from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from gexlab.api import app  # noqa: E402

_hosts = ["127.0.0.1", "localhost", "*.vercel.app"]
for _var in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
    if os.getenv(_var):
        _hosts.append(os.environ[_var])
_hosts += [h.strip() for h in os.getenv("GEXLAB_ALLOWED_HOSTS", "").split(",") if h.strip()]

from login_gate import LoginGate  # noqa: E402

app.user_middleware = [m for m in app.user_middleware if m.cls is not TrustedHostMiddleware]
app.add_middleware(LoginGate)  # login required for every page, API call and static file
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_hosts, www_redirect=False)
