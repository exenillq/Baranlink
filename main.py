# -*- coding: utf-8 -*-
import logging
import json
import requests
import os
import uuid
import asyncio
import redis
import io
import time
import urllib3
import random
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# --- تنظیم دامنه اصلی ---
DOMAIN_URL = os.getenv("DOMAIN_URL", "https://Ernull.bond")

# --- تولید لایسنس اختصاصی هوشمند ---
def generate_link_token(account_type="raw"):
    prefix = "R" if account_type == "raw" else "O"
    return f"BARANLINK-{prefix}-{str(uuid.uuid4())[:8].upper()}{str(uuid.uuid4())[:8].upper()}"

FIRST_NAMES = ["علی", "محمد", "یوسف", "امیر", "حسین", "رضا", "مهدی", "سارا", "زهرا", "مریم", "علیرضا", "عرفان", "نیما"]
LAST_NAMES = ["راد", "تهرانی", "حسینی", "پارسا", "دانش", "آریا", "محمدی", "کریمی", "احمدی", "ت زاده", "کمالی", "مجیدی"]

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
allowed_users_env = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(x.strip()) for x in allowed_users_env.split(",") if x.strip().isdigit()]

REDIS_URL = os.getenv("REDIS_URL")
PORT      = int(os.getenv("PORT", 8000))
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")

IRAN_PROXY = os.getenv("IRAN_PROXY", "")
SNAPPFOOD_PROXIES = {
    "http": IRAN_PROXY,
    "https": IRAN_PROXY
}

# --- تنظیمات عمومی اسنپ ---
SNAPP_MARKET_BASE_URL = "https://svc.snapp.market"
SNAPP_MARKET_CLIENT = os.getenv("SNAPP_MARKET_CLIENT", "PWA")
SNAPP_MARKET_DEVICE_TYPE = os.getenv("SNAPP_MARKET_DEVICE_TYPE", "PWA")
SNAPP_MARKET_APP_VERSION = os.getenv("SNAPP_MARKET_APP_VERSION", "1.397.62")
SNAPP_MARKET_LAT = os.getenv("SNAPP_MARKET_LAT", "35.773643")
SNAPP_MARKET_LONG = os.getenv("SNAPP_MARKET_LONG", "51.418311")
SNAPP_MARKET_SSO_CHANNEL = os.getenv("SNAPP_MARKET_SSO_CHANNEL", "food")
SNAPP_MARKET_VERIFY_TLS = False

DISCOUNT_CHECK_MIN_DELAY = 8.0
DISCOUNT_CHECK_MAX_DELAY = 15.0
DISCOUNT_CHECK_MAX_PAGES = max(1, min(20, int(os.getenv("DISCOUNT_CHECK_MAX_PAGES", "5"))))

try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logger.info("✅ اتصال به ردیس موفق بود.")
    else:
        redis_client = None
except Exception as e:
    redis_client = None
    logger.error(f"❌ خطا در ردیس: {e}")

BASE_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'fa',
    'content-type': 'application/json',
    'origin': 'https://snappfood.ir',
    'referer': 'https://snappfood.ir/',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
}

EXPRESS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR, fa;q=0.9,en;q=0.8",
    "User-Agent": BASE_HEADERS["user-agent"],
    "Origin": "https://snapp.market",
    "Referer": "https://snapp.market/"
}

discount_check_lock = asyncio.Lock()
purchase_check_lock = asyncio.Lock()

# ======================== وب‌سرور (پاسخ‌دهنده لینک‌ها) ========================
app = FastAPI(title="Baran Link System", docs_url=None, redoc_url=None)

@app.get("/api/BaranToken/{link_token}")
async def get_token(link_token: str, x_api_key: Optional[str] = Header(default=None)):
    if API_SECRET_KEY and x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not link_token.startswith("BARANLINK-"):
        raise HTTPException(status_code=400, detail="Invalid license key format")

    if not redis_client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        raw = redis_client.get(f"snappfood:license:{link_token}")
    except Exception as e:
        logger.error(f"خطا در ارتباط با دیتابیس: {e}")
        raise HTTPException(status_code=503, detail="Database error")

    if not raw:
        raise HTTPException(status_code=404, detail="Token not found")

    data = json.loads(raw)
    return JSONResponse(content={
        "success": True,
        "phone_number": data.get("phone_number"),
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "updated_at": data.get("updated_at")
    })

@app.get("/health")
async def health_check():
    db_ok = False
    if redis_client:
        try:
            redis_client.ping()
            db_ok = True
        except Exception:
            pass
    return {"status": "ok", "database": "connected" if db_ok else "disconnected"}

# =================================================================
# --- توابع ارتباط با سامانه‌ها ---

def _get_express_params(device_uid: str) -> dict:
    return {
        "client": SNAPP_MARKET_CLIENT,
        "deviceType": SNAPP_MARKET_DEVICE_TYPE,
        "appVersion": SNAPP_MARKET_APP_VERSION,
        "UDID": device_uid,
        "lat": SNAPP_MARKET_LAT,
        "long": SNAPP_MARKET_LONG
    }

def send_express_code(phone_number: str, device_uid: str) -> dict:
    url = f"{SNAPP_MARKET_BASE_URL}/mobile/v4/user/loginMobileWithNoPass"
    payload = {"captcha": "", "cellphone": phone_number, "optionalLoginToken": "true"}
    params = _get_express_params(device_uid)
    for attempt in range(3):
        try:
            res = requests.post(url, params=params, data=payload, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            if res.status_code == 424: return {'status': False, 'error': 'خطای ۴۲۴: نیاز به تغییر آی‌پی است'}
            try: return res.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f'خطای پروکسی/کلودفلر (کد {res.status_code})'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': 'ارتباط با سامانه برقرار نشد'}

def verify_express_code(phone_number: str, code: str, device_uid: str) -> dict:
    url = f"{SNAPP_MARKET_BASE_URL}/mobile/v2/user/loginMobileWithToken"
    payload = {"cellphone": phone_number, "code": code}
    params = _get_express_params(device_uid)
    for attempt in range(3):
        try:
            res = requests.post(url, params=params, data=payload, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            if res.status_code == 424: return {'http_status': 424, 'status': False, 'error': 'خطای ۴۲۴: نیاز به تغییر آی‌پی است'}
            try:
                data = res.json()
                data['http_status'] = res.status_code
                return data
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'http_status': res.status_code, 'status': False, 'error': f'خطای پروکسی/کلودفلر (کد {res.status_code})'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'http_status': 500, 'status': False, 'error': 'ارتباط با سامانه برقرار نشد'}

def register_express_user(phone_number: str, code: str, device_uid: str, first_name: str, last_name: str) -> dict:
    url = f"{SNAPP_MARKET_BASE_URL}/mobile/v1/user/registerWithOptionalPass"
    payload = {"firstname": first_name, "lastname": last_name, "cellphone": phone_number, "code": code}
    params = _get_express_params(device_uid)
    for attempt in range(3):
        try:
            res = requests.post(url, params=params, data=payload, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            if res.status_code == 424: return {'status': False, 'error': 'خطای ۴۲۴: نیاز به تغییر آی‌پی است'}
            try: return res.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f'خطای پروکسی/کلودفلر (کد {res.status_code})'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': 'ارتباط با سامانه برقرار نشد'}

def send_food_code(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            if response.status_code == 424: return {'status': False, 'error': 'خطای ۴۲۴: نیاز به تغییر آی‌پی است'}
            try: return response.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f'خطای پروکسی/کلودفلر (کد {response.status_code})'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': "ارتباط با سامانه برقرار نشد"}

def verify_food_code(phone_number: str, code: str, device_uid: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": device_uid,
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            if response.status_code == 424: return {'http_status': 424, 'error': 'خطای ۴۲۴: نیاز به تغییر آی‌پی است'}
            try:
                data = response.json()
                data['http_status'] = response.status_code
                return data
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'http_status': response.status_code, 'error': f'خطای پروکسی/کلودفلر (کد {response.status_code})'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'http_status': 500, 'error': "ارتباط با سامانه برقرار نشد"}

def register_food_user(phone_number: str, code: str, device_uid: str, first_name: str, last_name: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "firstName": first_name, "lastName": last_name,
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": device_uid,
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            if response.status_code == 424: return {'status': False, 'error': 'خطای ۴۲۴: نیاز به تغییر آی‌پی است'}
            try: return response.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f'خطای پروکسی/کلودفلر (کد {response.status_code})'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': "ارتباط با سامانه برقرار نشد"}

def refresh_short_token(short_refresh_token: str) -> dict:
    device_uid = str(uuid.uuid4())
    headers = BASE_HEADERS.copy()
    # هدر authority حذف شد تا فایروال به درخواست گیر ندهد
    payload = {
        "refreshToken": short_refresh_token, "grantType": "RefreshToken",
        "data": {
            "time": int(time.time()), "device_uid": device_uid,
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    for attempt in range(3):
        try:
            res = requests.post("https://user.snappfood.ir/v1/auth/token", json=payload, headers=headers, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=20)
            if res.status_code == 424: return {'status': False, 'error': 'خطای ۴۲۴: نیاز به تغییر آی‌پی است'}
            try: data = res.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f'پروکسی/فایروال (کد {res.status_code})'}
                
            if res.status_code == 200:
                resp_data = data.get("data", {}) or {}
                new_access  = resp_data.get("accessToken")
                new_refresh = resp_data.get("refreshToken") or short_refresh_token
                if new_access: return {'status': True, 'data': {'accessToken': new_access, 'refreshToken': new_refresh}}
                return {'status': False, 'error': 'عدم دریافت دسترسی جدید.'}
                
            err_msg = data.get("error") or data.get("message") or "نامشخص"
            return {'status': False, 'error': err_msg}
        except Exception as e:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': 'ارتباط با سامانه برقرار نشد'}

def exchange_food_token_for_market_token(access_token: str, device_uid: str) -> dict:
    params = {"token": access_token, "sso_channel": SNAPP_MARKET_SSO_CHANNEL, **_get_express_params(device_uid)}
    try:
        # پروکسی به چکرها برگشت تا سرورهای خارجی (مثل Railway) بلاک نشن
        response = requests.get(f"{SNAPP_MARKET_BASE_URL}/mobile/v2/user/snapp-sso", params=params, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=20)
        if response.status_code == 424: return {"status": False, "retryable": False, "error_code": "خطای ۴۲۴ (نیاز به آی‌پی ایران)"}
        if response.status_code != 200: return {"status": False, "retryable": response.status_code in {401, 403, 502}, "error_code": f"خطای دسترسی {response.status_code}"}
        try: payload = response.json() or {}
        except ValueError: return {"status": False, "retryable": True, "error_code": f"پروکسی/فایروال ({response.status_code})"}
        
        market_token = payload.get("data", {}).get("oauth2_token", {}).get("access_token")
        if not market_token: return {"status": False, "retryable": False, "error_code": "دسترسی دریافت نشد"}
        return {"status": True, "access_token": market_token}
    except requests.RequestException:
        return {"status": False, "retryable": True, "error_code": "ارتباط برقرار نشد"}

# ======================== چکر سابقه خرید ========================
def fetch_market_purchase_status(market_access_token: str, device_uid: str) -> dict:
    headers = EXPRESS_HEADERS.copy()
    headers["Authorization"] = f"Bearer {market_access_token}"
    has_real_purchase = False
    
    urls = [
        "https://api.snapp.express/mobile/v1/order/reorder",
        "https://api.snapp.express/mobile/v4/order/external/reorder",
        "https://api.snapp.express/oms/v1/orders/history"
    ]
    
    try:
        for url in urls:
            params = _get_express_params(device_uid)
            params["vendorSuperType"] = "SUPERMARKET"
            if "oms" in url:
                params["active"] = "false"
            else:
                params.update({"page": "0", "size": "20", "split_page": "0"})

            response = requests.get(url, params=params, headers=headers, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            if response.status_code == 424:
                return {"status": False, "retryable": False, "error_code": "خطای ۴۲۴"}
            if response.status_code != 200:
                return {"status": False, "retryable": response.status_code in {401, 403, 502}, "error_code": f"خطا {response.status_code}"}
            
            try: payload = response.json() or {}
            except ValueError: return {"status": False, "retryable": True, "error_code": f"پروکسی/فایروال ({response.status_code})"}
            
            dt = payload.get("data", {})
            orders = dt.get("orders", []) if isinstance(dt, dict) else (dt if isinstance(dt, list) else [])
            for order in orders:
                if order.get("isCanceled") is False or order.get("status") in ["DELIVERED", "COMPLETED", "SUCCESS"]:
                    has_real_purchase = True
                    break
            if has_real_purchase:
                break
        return {"status": True, "has_purchase": has_real_purchase}
    except requests.RequestException:
        return {"status": False, "retryable": True, "error_code": "ارتباط برقرار نشد"}

def check_account_purchases(record: dict) -> dict:
    access_token = record.get("access_token")
    refresh_token = record.get("refresh_token")
    device_uid = record.get("device_uid") or str(uuid.uuid4())
    if not access_token: return {"status": False, "error_code": "عدم دسترسی"}
    for attempt in range(2):
        sso_result = exchange_food_token_for_market_token(access_token, device_uid)
        if sso_result.get("status"):
            purchase_result = fetch_market_purchase_status(sso_result["access_token"], device_uid)
            if purchase_result.get("status"): 
                return {"status": True, "has_purchase": purchase_result.get("has_purchase", False), "device_uid": device_uid, "refreshed": attempt == 1}
            should_refresh = purchase_result.get("retryable", False)
        else:
            should_refresh = sso_result.get("retryable", False)
            
        if not should_refresh or attempt != 0 or not refresh_token:
            return {"status": False, "error_code": (purchase_result.get("error_code") if sso_result.get("status") else sso_result.get("error_code"))}
            
        refresh_result = refresh_short_token(refresh_token)
        refresh_data = refresh_result.get("data") or {}
        new_access = refresh_data.get("accessToken")
        new_refresh = refresh_data.get("refreshToken") or refresh_token
        if not refresh_result.get("status") or not new_access:
            err = refresh_result.get("error", "ناموفق")
            return {"status": False, "error_code": f"عدم تمدید ({err})"}
        access_token, refresh_token = new_access, new_refresh
        record["access_token"], record["refresh_token"], record["device_uid"] = new_access, new_refresh, device_uid
    return {"status": False, "error_code": "بررسی ناموفق"}

# ======================== چکر تخفیف ========================
def fetch_market_vouchers(market_access_token: str, device_uid: str) -> dict:
    headers = EXPRESS_HEADERS.copy()
    headers["Authorization"] = f"Bearer {market_access_token}"
    vouchers = []
    try:
        for page in range(1, DISCOUNT_CHECK_MAX_PAGES + 1):
            params = {"filterType": "all", "page": page, "pageSize": 10}
            response = requests.get(f"{SNAPP_MARKET_BASE_URL}/belladonna/api/v1/vouchers", params=params, headers=headers, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=20)
            if response.status_code == 424: return {"status": False, "retryable": False, "error_code": "خطای ۴۲۴"}
            if response.status_code != 200: return {"status": False, "retryable": response.status_code in {401, 403, 502}, "error_code": f"خطا {response.status_code}"}
            try: payload = response.json() or {}
            except ValueError: return {"status": False, "retryable": True, "error_code": f"پروکسی/فایروال ({response.status_code})"}
            if isinstance(payload, dict):
                page_items = payload.get("vouchers") or []
                if isinstance(page_items, list): vouchers.extend(item for item in page_items if isinstance(item, dict))
                if not payload.get("hasMore"): break
            else: return {"status": False, "retryable": False, "error_code": "پاسخ نامعتبر"}
        return {"status": True, "vouchers": vouchers}
    except requests.RequestException:
        return {"status": False, "retryable": True, "error_code": "ارتباط برقرار نشد"}

def check_account_discounts(record: dict) -> dict:
    access_token = record.get("access_token")
    refresh_token = record.get("refresh_token")
    device_uid = record.get("device_uid") or str(uuid.uuid4())
    if not access_token: return {"status": False, "error_code": "عدم دسترسی"}
    for attempt in range(2):
        sso_result = exchange_food_token_for_market_token(access_token, device_uid)
        if sso_result.get("status"):
            voucher_result = fetch_market_vouchers(sso_result["access_token"], device_uid)
            if voucher_result.get("status"): return {"status": True, "vouchers": voucher_result.get("vouchers", []), "device_uid": device_uid, "refreshed": attempt == 1}
            should_refresh = voucher_result.get("retryable", False)
        else:
            should_refresh = sso_result.get("retryable", False)
        if not should_refresh or attempt != 0 or not refresh_token:
            return {"status": False, "error_code": (voucher_result.get("error_code") if sso_result.get("status") else sso_result.get("error_code"))}
            
        refresh_result = refresh_short_token(refresh_token)
        refresh_data = refresh_result.get("data") or {}
        new_access = refresh_data.get("accessToken")
        new_refresh = refresh_data.get("refreshToken") or refresh_token
        if not refresh_result.get("status") or not new_access:
            err = refresh_result.get("error", "ناموفق")
            return {"status": False, "error_code": f"عدم تمدید ({err})"}
        access_token, refresh_token = new_access, new_refresh
        record["access_token"], record["refresh_token"], record["device_uid"] = new_access, new_refresh, device_uid
    return {"status": False, "error_code": "بررسی ناموفق"}

# ======================== گزارش‌گیری ========================
def get_account_type(record: dict) -> str:
    return "old" if record.get("account_type") == "old" else "raw"

def get_database_account_stats() -> dict:
    stats = {"total": 0, "raw": 0, "old": 0}
    if not redis_client: return stats
    for key in redis_client.keys("snappfood:license:*"):
        try:
            raw = redis_client.get(key)
            record = json.loads(raw) if raw else {}
            account_type = get_account_type(record)
            stats["total"] += 1
            stats[account_type] += 1
        except Exception:
            stats["total"] += 1
            stats["raw"] += 1
    return stats

def _text_value(value, default="نامشخص") -> str:
    if value is None or value == "": return default
    return str(value).replace("\r", " ").replace("\n", " ").strip()

def build_discount_report(results: list[dict], account_type: str) -> str:
    account_title = "اکانت‌های خام (raw)" if account_type == "raw" else "اکانت‌های قدیمی (old)"
    lines = [f"گزارش چکر تخفیف - {account_title}", f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "=" * 50, ""]
    for idx, r in enumerate(results, 1):
        lines.extend([f"اکانت {idx} | لینک: {DOMAIN_URL}/{r.get('link_token')} | شماره: {r.get('phone_number')}"])
        if r.get("status") != "ok": lines.append(f"وضعیت: خطا ({_text_value(r.get('error_code'))})")
        elif not r.get("vouchers"): lines.append("تخفیف: یافت نشد")
        else:
            lines.append(f"تخفیف‌ها ({len(r.get('vouchers'))} عدد):")
            for v in r.get('vouchers'): lines.append(f" - {v.get('code')} | {v.get('title')} | انقضا: {v.get('expiryDate')}")
        lines.extend(["-" * 50, ""])
    return "\n".join(lines)

def build_purchase_report(results: list[dict], account_type: str) -> str:
    lines = [
        f"لیست اکانت‌های صفر (بدون سابقه خرید) - بخش {account_type}",
        f"تاریخ بررسی: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 50, ""
    ]
    zero_count = 0
    for r in results:
        if r.get("status") == "ok" and not r.get("has_purchase"):
            zero_count += 1
            lines.append(f"شماره: {r.get('phone_number')} | لینک: {DOMAIN_URL}/{r.get('link_token')}")
            
    lines.extend(["", "=" * 50, f"تعداد کل اکانت‌های صفر: {zero_count}"])
    return "\n".join(lines)

async def safe_edit_progress(progress_message, text: str) -> None:
    try: await progress_message.edit_text(text, parse_mode="Markdown")
    except Exception: pass

# ======================== تسک چکر خودکار پس‌زمینه ========================
async def auto_discount_checker_loop(bot):
    await asyncio.sleep(10) 
    
    while True:
        try:
            if not redis_client:
                await asyncio.sleep(60)
                continue
                
            config_raw = redis_client.get("config:auto_discount")
            config = json.loads(config_raw) if config_raw else {"enabled": False, "interval": 24}
            
            if config.get("enabled"):
                keys = redis_client.keys("snappfood:license:*")
                if keys:
                    logger.info("🤖 شروع چرخه جدید چکر خودکار تخفیف...")
                    for key in keys:
                        config_raw = redis_client.get("config:auto_discount")
                        config = json.loads(config_raw) if config_raw else {"enabled": False, "interval": 24}
                        if not config.get("enabled"):
                            break
                            
                        raw = redis_client.get(key)
                        if not raw: continue
                        record = json.loads(raw)
                        
                        result = await asyncio.to_thread(check_account_discounts, record)
                        
                        if result.get("refreshed") and result.get("status") and record.get("access_token"):
                            record["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            redis_client.set(key, json.dumps(record, ensure_ascii=False))
                        
                        vouchers = result.get("vouchers", [])
                        if result.get("status") in [True, "ok"] and vouchers:
                            stored_token = record.get("link_token") or record.get("license_key") or key.split(":")[-1]
                            phone = record.get("phone_number", "نامشخص")
                            
                            msg = (
                                f"🎉 *تخفیف جدید پیدا شد! (چکر خودکار)*\n\n"
                                f"📱 شماره: `{phone}`\n"
                                f"🔗 لینک: `{DOMAIN_URL}/{stored_token}`\n"
                                f"🎁 تعداد تخفیف: `{len(vouchers)}`\n"
                            )
                            for v in vouchers:
                                msg += f"\n🔸 کدتخفیف: `{v.get('code')}`\n🏷 عنوان: {_text_value(v.get('title'))}\n⏳ انقضا: {_text_value(v.get('expiryDateFormatted') or v.get('expiryDate'))}\n"
                            
                            for admin_id in ALLOWED_USER_IDS:
                                try:
                                    await bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
                                except Exception: 
                                    pass
                                
                        await asyncio.sleep(random.uniform(30.0, 60.0))
                        
                interval_hours = config.get("interval", 24)
                logger.info(f"🤖 چرخه به اتمام رسید. خواب برای {interval_hours} ساعت...")
                
                wait_seconds = interval_hours * 3600
                slept = 0
                while slept < wait_seconds:
                    config_raw = redis_client.get("config:auto_discount")
                    config = json.loads(config_raw) if config_raw else {"enabled": False, "interval": 24}
                    if not config.get("enabled"):
                        break
                    await asyncio.sleep(60)
                    slept += 60
            else:
                await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"خطا در حلقه چکر خودکار: {e}")
            await asyncio.sleep(60)

# ======================== پردازش‌های پس‌زمینه (Async) ========================
async def process_discount_check(chat_id: int, bot, account_type: str, mode: str = "all", count: int = 0) -> None:
    async with discount_check_lock:
        if not redis_client:
            await bot.send_message(chat_id, "❌ دیتابیس متصل نیست!")
            return
            
        accounts_with_date = []
        now_time = datetime.now()
        for key in redis_client.keys("snappfood:license:*"):
            try:
                record = json.loads(redis_client.get(key) or "{}")
                if get_account_type(record) == account_type:
                    if mode == "recent":
                        ct = record.get("created_at")
                        if ct and (now_time - datetime.strptime(ct, '%Y-%m-%d %H:%M:%S')).total_seconds() <= 86400:
                            accounts_with_date.append((key, ct))
                    elif mode == "unchecked":
                        if not record.get("discount_checked", False):
                            accounts_with_date.append((key, record.get("created_at", "")))
                    else:
                        accounts_with_date.append((key, record.get("created_at", "")))
            except Exception: pass
            
        accounts_with_date.sort(key=lambda x: x[1], reverse=True)
        keys = [k for k, _ in accounts_with_date]
        
        if mode == "custom" and count > 0:
            keys = keys[:count]
            
        if not keys:
            await bot.send_message(chat_id, f"ℹ️ رکوردی برای بررسی یافت نشد.")
            return

        if mode == "recent": check_mode_text = "تازه‌ها (۲۴ ساعت اخیر)"
        elif mode == "unchecked": check_mode_text = "خطوط بررسی‌نشده"
        elif mode == "custom": check_mode_text = f"{len(keys)} اکانت اخیر"
        else: check_mode_text = "همه خطوط"

        progress_msg = await bot.send_message(chat_id, f"🔎 *چکر تخفیف در حال اجرا...*\nحالت: `{check_mode_text}`\nپیشرفت: `0/{len(keys)}`", parse_mode="Markdown")
        results = []
        for idx, key in enumerate(keys, 1):
            token = key.split(":")[-1]
            try:
                record = json.loads(redis_client.get(key) or "{}")
                result = await asyncio.to_thread(check_account_discounts, record)
                
                if result.get("status") in [True, "ok"] or result.get("status") == True:
                    record["discount_checked"] = True

                if result.get("status") or result.get("refreshed"):
                    record["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    redis_client.set(key, json.dumps(record, ensure_ascii=False))
                
                stored_token = record.get("link_token") or record.get("license_key") or token
                results.append({
                    "status": "ok" if result.get("status") else "error", "error_code": result.get("error_code"),
                    "vouchers": result.get("vouchers", []), "phone_number": record.get("phone_number"), "link_token": stored_token,
                })
            except: results.append({"status": "error", "error_code": "خطا در ارتباط", "phone_number": "نامشخص", "link_token": token})
            
            await safe_edit_progress(progress_msg, f"🔎 *چکر تخفیف در حال اجرا...*\nحالت: `{check_mode_text}`\nپیشرفت: `{idx}/{len(keys)}`\nشماره: `{record.get('phone_number','نامشخص')}`")
            if idx < len(keys): await asyncio.sleep(random.uniform(DISCOUNT_CHECK_MIN_DELAY, DISCOUNT_CHECK_MAX_DELAY))

        doc = io.BytesIO(build_discount_report(results, account_type).encode("utf-8"))
        doc.name = f"Discounts_{account_type}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await bot.send_document(chat_id, document=doc, caption=f"✅ بررسی تخفیف‌ها پایان یافت.\nموارد بررسی شده: `{len(keys)}`", parse_mode="Markdown", reply_markup=kb_admin_main())

async def process_purchase_check(chat_id: int, bot, account_type: str, mode: str = "all", count: int = 0) -> None:
    async with purchase_check_lock:
        if not redis_client:
            await bot.send_message(chat_id, "❌ دیتابیس متصل نیست!")
            return
            
        accounts_with_date = []
        now_time = datetime.now()
        for key in redis_client.keys("snappfood:license:*"):
            try:
                record = json.loads(redis_client.get(key) or "{}")
                if get_account_type(record) == account_type:
                    if mode == "recent":
                        ct = record.get("created_at")
                        if ct and (now_time - datetime.strptime(ct, '%Y-%m-%d %H:%M:%S')).total_seconds() <= 86400:
                            accounts_with_date.append((key, ct))
                    elif mode == "unchecked":
                        if not record.get("purchase_checked", False):
                            accounts_with_date.append((key, record.get("created_at", "")))
                    else:
                        accounts_with_date.append((key, record.get("created_at", "")))
            except Exception: pass
            
        accounts_with_date.sort(key=lambda x: x[1], reverse=True)
        keys = [k for k, _ in accounts_with_date]
        
        if mode == "custom" and count > 0:
            keys = keys[:count]
            
        if not keys:
            await bot.send_message(chat_id, f"ℹ️ رکوردی برای بررسی یافت نشد.")
            return

        if mode == "recent": check_mode_text = "تازه‌ها (۲۴ ساعت اخیر)"
        elif mode == "unchecked": check_mode_text = "خطوط بررسی‌نشده"
        elif mode == "custom": check_mode_text = f"{len(keys)} اکانت اخیر"
        else: check_mode_text = "همه خطوط"

        progress_msg = await bot.send_message(chat_id, f"🛒 *چکر سابقه خرید در حال اجرا...*\nحالت: `{check_mode_text}`\nپیشرفت: `0/{len(keys)}`\nصفر: `0` | خریددار: `0` | خطا: `0`", parse_mode="Markdown")
        results = []
        z_count, p_count, e_count = 0, 0, 0
        
        for idx, key in enumerate(keys, 1):
            token = key.split(":")[-1]
            try:
                record = json.loads(redis_client.get(key) or "{}")
                result = await asyncio.to_thread(check_account_purchases, record)
                
                if result.get("status") in [True, "ok"] or result.get("status") == True:
                    record["purchase_checked"] = True

                if result.get("status") or result.get("refreshed"):
                    record["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    redis_client.set(key, json.dumps(record, ensure_ascii=False))
                
                stored_token = record.get("link_token") or record.get("license_key") or token
                
                if result.get("status"):
                    if result.get("has_purchase"): p_count += 1
                    else: z_count += 1
                else: e_count += 1

                results.append({
                    "status": "ok" if result.get("status") else "error", "error_code": result.get("error_code"),
                    "has_purchase": result.get("has_purchase", False), "phone_number": record.get("phone_number"), "link_token": stored_token,
                })
            except: 
                e_count += 1
                results.append({"status": "error", "error_code": "خطا در ارتباط", "phone_number": "نامشخص", "link_token": token})
            
            await safe_edit_progress(progress_msg, f"🛒 *چکر سابقه خرید در حال اجرا...*\nحالت: `{check_mode_text}`\nپیشرفت: `{idx}/{len(keys)}`\nشماره فعلی: `{record.get('phone_number','نامشخص')}`\n🎁 صفر: `{z_count}` | ⚠️ خریددار: `{p_count}` | ❌ خطا: `{e_count}`")
            if idx < len(keys): await asyncio.sleep(random.uniform(DISCOUNT_CHECK_MIN_DELAY, DISCOUNT_CHECK_MAX_DELAY))

        doc = io.BytesIO(build_purchase_report(results, account_type).encode("utf-8"))
        doc.name = f"Zero_Accounts_{account_type}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await bot.send_document(chat_id, document=doc, caption=f"✅ بررسی سابقه خرید پایان یافت.\nموارد بررسی شده: `{len(keys)}`\n\n🎁 اکانت صفر: `{z_count}`\n⚠️ خریددار: `{p_count}`\n❌ خطا/مسدود: `{e_count}`", parse_mode="Markdown", reply_markup=kb_admin_main())

async def process_database_rebuild(chat_id: int, bot, count: int):
    if not redis_client:
        await bot.send_message(chat_id, "❌ دیتابیس متصل نیست!")
        return
    keys = redis_client.keys("snappfood:license:*")
    if not keys:
        await bot.send_message(chat_id, "ℹ️ هیچ اتصالی یافت نشد.")
        return
        
    accounts = []
    for k in keys:
        try:
            data = json.loads(redis_client.get(k) or "{}")
            accounts.append((k, data.get("created_at", "")))
        except:
            accounts.append((k, ""))
            
    accounts.sort(key=lambda x: x[1], reverse=True)
    target_keys = [k for k, _ in accounts[:count]]

    success_count, fail_count = 0, 0
    await bot.send_message(chat_id, f"🔄 *شروع بازسازی اتصال‌ها*\nمجموع درخواست: `{len(target_keys)}`\n⏳ صبر کنید...", parse_mode='Markdown')
    for key in target_keys:
        try:
            raw = redis_client.get(key)
            if not raw:
                fail_count += 1
                continue
            data = json.loads(raw)
            if not data.get("phone_number") or not data.get("refresh_token"):
                fail_count += 1; continue
            res = await asyncio.to_thread(refresh_short_token, data.get("refresh_token"))
            new_data = res.get('data') or {}
            if res.get('status') and new_data.get('accessToken'):
                data["access_token"] = new_data.get('accessToken')
                data["refresh_token"] = new_data.get('refreshToken') or data.get("refresh_token")
                data["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                redis_client.set(key, json.dumps(data, ensure_ascii=False))
                success_count += 1
            else: fail_count += 1
        except: fail_count += 1
        await asyncio.sleep(2)
    await bot.send_message(chat_id, f"✅ *بازسازی اتصال‌ها پایان یافت*\n🟢 موفق: `{success_count}` | 🔴 ناموفق: `{fail_count}`", parse_mode='Markdown')

# ======================== کیبوردهای تلگرام ========================
def kb_cancel() -> InlineKeyboardMarkup: return InlineKeyboardMarkup([[InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])
def kb_resend_step1() -> InlineKeyboardMarkup: return InlineKeyboardMarkup([[InlineKeyboardButton("🔄  ارسال مجدد کد مرحله اول", callback_data='resend_code_1')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])
def kb_resend_step2() -> InlineKeyboardMarkup: return InlineKeyboardMarkup([[InlineKeyboardButton("🔄  ارسال مجدد کد مرحله دوم", callback_data='resend_code_2')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])
def kb_next_or_finish() -> InlineKeyboardMarkup: return InlineKeyboardMarkup([[InlineKeyboardButton("➕  ثبت لینک خام بعدی", callback_data='next_line')], [InlineKeyboardButton("✅  پایان", callback_data='finish_session')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])
def kb_old_next_or_finish() -> InlineKeyboardMarkup: return InlineKeyboardMarkup([[InlineKeyboardButton("➕  ثبت اکانت قدیمی بعدی", callback_data='old_next_line')], [InlineKeyboardButton("✅  پایان", callback_data='old_finish_session')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])
def kb_back_to_admin() -> InlineKeyboardMarkup: return InlineKeyboardMarkup([[InlineKeyboardButton("🔙  بازگشت به پنل", callback_data='admin_back')]])
def kb_old_resend_step() -> InlineKeyboardMarkup: return InlineKeyboardMarkup([[InlineKeyboardButton("🔄  ارسال مجدد کد", callback_data='old_resend_code')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])

def kb_check_options(action: str, account_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 بررسی خطوط جدید (بررسی‌نشده)", callback_data=f'admin_run_{action}_unchecked_{account_type}')],
        [InlineKeyboardButton("🔢 بررسی تعداد دلخواه (از آخر)", callback_data=f'admin_checkcustom_{action}_{account_type}')],
        [InlineKeyboardButton("🔍 بررسی همه (از ابتدا)", callback_data=f'admin_run_{action}_all_{account_type}')],
        [InlineKeyboardButton("🕒 بررسی خطوط اخیر (۲۴ ساعت)", callback_data=f'admin_run_{action}_recent_{account_type}')],
        [InlineKeyboardButton("🔙 بازگشت به پنل", callback_data='admin_back')]
    ])

def kb_auto_checker_menu(config) -> InlineKeyboardMarkup:
    status = "🟢 روشن" if config.get("enabled") else "🔴 خاموش"
    interval = config.get("interval", 24)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"وضعیت: {status} (تغییر)", callback_data='admin_autocheck_toggle')],
        [InlineKeyboardButton(f"⏳ تنظیم زمان (فعلی: {interval} ساعت)", callback_data='admin_autocheck_setint')],
        [InlineKeyboardButton("🔙 بازگشت به پنل", callback_data='admin_back')]
    ])

def kb_admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊  آمار سیستم", callback_data='admin_stats'), InlineKeyboardButton("🔑  گزارش ارتباطات", callback_data='admin_extract_tokens')],
        [InlineKeyboardButton("➕  تولید لینک جدید", callback_data='admin_new_license'), InlineKeyboardButton("➕  ثبت اکانت قدیمی", callback_data='admin_old_license')],
        [InlineKeyboardButton("📥  دریافت ۲۰تایی خام", callback_data='admin_get_list_raw'), InlineKeyboardButton("📥  دریافت ۲۰تایی قدیمی", callback_data='admin_get_list_old')],
        [InlineKeyboardButton("🔄  بازسازی اتصال‌ها", callback_data='admin_rebuild_start')],
        [InlineKeyboardButton("🎁 چکر تخفیف (خام)", callback_data='admin_checkmenu_discount_raw'), InlineKeyboardButton("🎁 چکر تخفیف (قدیمی)", callback_data='admin_checkmenu_discount_old')],
        [InlineKeyboardButton("🛒 چکر خرید (خام)", callback_data='admin_checkmenu_purchase_raw'), InlineKeyboardButton("🛒 چکر خرید (قدیمی)", callback_data='admin_checkmenu_purchase_old')],
        [InlineKeyboardButton("🤖 تنظیمات چکر خودکار", callback_data='admin_autocheck_menu')],
        [InlineKeyboardButton("🗑  حذف گروهی قدیمی‌ها", callback_data='batch_delete_old_start')],
        [InlineKeyboardButton("📥  فایل پشتیبان", callback_data='admin_extract'), InlineKeyboardButton("🗑  حذف تکی", callback_data='admin_delete_hint')]
    ])

# ======================== هندلر اصلی و استارت ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.info(f"➡️ دریافت پیام استارت از آیدی: {user_id}")
    
    if user_id not in ALLOWED_USER_IDS:
        logger.warning(f"⛔️ آیدی {user_id} مجاز نیست!")
        await update.message.reply_text(
            f"⛔️ شما دسترسی به این پنل را ندارید.\n"
            f"آیدی عددی شما: `{user_id}`\n\n"
            f"اگر مدیر هستید، باید این عدد را در سیستم ثبت کنید.", 
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data.clear()
    stats = get_database_account_stats()
    text = (f"⚙️  *پنل مدیریت Baran*\n\n🗄  وضعیت اطلاعات: {'🟢 متصل' if redis_client else '🔴 قطع'}\n"
            f"📊  مجموع لینک‌ها: `{stats['total']}`\n🟠  خام: `{stats['raw']}` | 🔵  قدیمی: `{stats['old']}`")
    await update.message.reply_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')
    return ConversationHandler.END

# ======================== مراحل تلگرام ========================
ASK_PHONE, ASK_CODE_STEP_1, ASK_CODE_STEP_2, ASK_NEXT_ACTION = range(4)
OLD_ASK_PHONE, OLD_ASK_CODE, OLD_ASK_NEXT_ACTION = range(4, 7)
ASK_BATCH_DELETE_COUNT = 7
ASK_REBUILD_COUNT = 8
ASK_CHECKER_COUNT = 9
ASK_AUTO_INTERVAL = 10

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🚫 عملیات لغو شد.\n/start را ارسال کنید.")
    else: await update.message.reply_text("🚫 عملیات لغو شد.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def exit_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("⚙️ *پنل مدیریت*", reply_markup=kb_admin_main(), parse_mode="Markdown")
    else: await update.message.reply_text("⚙️ *پنل مدیریت*", reply_markup=kb_admin_main(), parse_mode="Markdown")
    return ConversationHandler.END

# --- توابع ربات تلگرام (ثبت و ورود) ---
async def start_raw_license_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data.clear(); context.user_data['session_phones'] = []
    await update.callback_query.edit_message_text("➕  *تولید لینک ورود جدید*\n\n📱  شماره موبایل مشتری را وارد کنید:\n_(فرمت: `09XXXXXXXXX`)_", reply_markup=kb_cancel(), parse_mode='Markdown')
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text.strip()
    if not (phone_number.startswith("09") and len(phone_number) == 11 and phone_number.isdigit()):
        await update.message.reply_text("⚠️  شماره نامعتبر است.", reply_markup=kb_cancel())
        return ASK_PHONE
    context.user_data['phone_number'] = phone_number
    context.user_data['device_uid'] = str(uuid.uuid4())
    wait_msg = await update.message.reply_text("⏳  درحال ارسال کد مرحله اول...")
    res = await asyncio.to_thread(send_express_code, phone_number, context.user_data['device_uid'])
    if res.get('status') or res.get('success'):
        await wait_msg.delete()
        await update.message.reply_text(f"✅  *کد ارسال شد*\n\n📲  کد ۵ رقمی را وارد کنید:", reply_markup=kb_resend_step1(), parse_mode='Markdown')
        return ASK_CODE_STEP_1
    await wait_msg.edit_text(f"❌  بروز مشکل: {res.get('error')}"); return ConversationHandler.END

async def ask_code_step_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    if not code.isdigit(): return ASK_CODE_STEP_1
    wait_msg = await update.message.reply_text("⏳  درحال بررسی...")
    res = await asyncio.to_thread(verify_express_code, context.user_data['phone_number'], code, context.user_data['device_uid'])
    if res.get('http_status') == 200:
        if not res.get('data', {}).get('is_registered', False):
            await asyncio.to_thread(register_express_user, context.user_data['phone_number'], code, context.user_data['device_uid'], random.choice(FIRST_NAMES), random.choice(LAST_NAMES))
        food_res = await asyncio.to_thread(send_food_code, context.user_data['phone_number'])
        if food_res.get('status') or food_res.get('success'):
            await wait_msg.edit_text("🔐  *کد تایید نهایی ارسال شد*\n\n📲  آخرین کد پیامک شده را وارد کنید:", reply_markup=kb_resend_step2(), parse_mode='Markdown')
            return ASK_CODE_STEP_2
    await wait_msg.edit_text(f"⚠️ کد نامعتبر است."); return ASK_CODE_STEP_1

async def ask_code_step_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    wait_msg = await update.message.reply_text("⏳  درحال صدور لینک...")
    res = await asyncio.to_thread(verify_food_code, context.user_data['phone_number'], code, context.user_data['device_uid'])
    if res.get('http_status') == 200:
        access = res.get('data', {}).get('accessToken')
        if not access:
            reg_res = await asyncio.to_thread(register_food_user, context.user_data['phone_number'], code, context.user_data['device_uid'], random.choice(FIRST_NAMES), random.choice(LAST_NAMES))
            access = reg_res.get('data', {}).get('accessToken')
            if not access: await wait_msg.edit_text("❌ خطا در ثبت نام خودکار"); return ConversationHandler.END
        await wait_msg.delete()
        link_token = generate_link_token("raw")
        redis_client.set(f"snappfood:license:{link_token}", json.dumps({"phone_number": context.user_data['phone_number'], "device_uid": context.user_data['device_uid'], "access_token": access, "refresh_token": res.get('data', {}).get('refreshToken'), "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "link_token": link_token, "account_type": "raw"}, ensure_ascii=False))
        context.user_data.setdefault('session_phones', []).append(f"`{DOMAIN_URL}/{link_token}`")
        await update.message.reply_text(f"✅  *لینک مشتری:*\n`{DOMAIN_URL}/{link_token}`\n\nمرحله بعد:", reply_markup=kb_next_or_finish(), parse_mode='Markdown')
        return ASK_NEXT_ACTION
    await wait_msg.edit_text("⚠️ کد نامعتبر است."); return ASK_CODE_STEP_2

# --- هندلرهای دکمه‌های Callback ---
async def resend_code_1_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("ارسال مجدد..."); await asyncio.to_thread(send_express_code, context.user_data.get('phone_number'), context.user_data.get('device_uid'))
    return ASK_CODE_STEP_1

async def resend_code_2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("ارسال مجدد..."); await asyncio.to_thread(send_food_code, context.user_data.get('phone_number'))
    return ASK_CODE_STEP_2

# --- بخش اکانت قدیمی با چرخه تکرار ---
async def old_license_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer(); context.user_data.clear(); context.user_data['old_session_phones'] = []
    await update.callback_query.edit_message_text("➕  *ثبت اکانت قدیمی*\n\n📱  شماره موبایل:", reply_markup=kb_cancel(), parse_mode='Markdown')
    return OLD_ASK_PHONE

async def old_ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not phone.isdigit(): return OLD_ASK_PHONE
    context.user_data['phone_number'] = phone; context.user_data['device_uid'] = str(uuid.uuid4())
    wait_msg = await update.message.reply_text("⏳ ارسال کد...")
    res = await asyncio.to_thread(send_food_code, phone)
    if res.get('status') or res.get('success'):
        await wait_msg.edit_text("✅  *کد ارسال شد*\n\n📲  کد را وارد کنید:", reply_markup=kb_old_resend_step(), parse_mode='Markdown')
        return OLD_ASK_CODE
    await wait_msg.edit_text(f"❌ مشکل در ارتباط: {res.get('error')}"); return ConversationHandler.END

async def old_ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    wait_msg = await update.message.reply_text("⏳ درحال ثبت...")
    res = await asyncio.to_thread(verify_food_code, context.user_data['phone_number'], code, context.user_data['device_uid'])
    if res.get('http_status') == 200:
        access = res.get('data', {}).get('accessToken')
        if not access:
            reg_res = await asyncio.to_thread(register_food_user, context.user_data['phone_number'], code, context.user_data['device_uid'], random.choice(FIRST_NAMES), random.choice(LAST_NAMES))
            access = reg_res.get('data', {}).get('accessToken')
            if not access: await wait_msg.edit_text("❌ خطا در ثبت"); return ConversationHandler.END
        await wait_msg.delete()
        link_token = generate_link_token("old")
        redis_client.set(f"snappfood:license:{link_token}", json.dumps({"phone_number": context.user_data['phone_number'], "device_uid": context.user_data['device_uid'], "access_token": access, "refresh_token": res.get('data', {}).get('refreshToken'), "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "link_token": link_token, "account_type": "old"}, ensure_ascii=False))
        context.user_data.setdefault('old_session_phones', []).append(f"`{DOMAIN_URL}/{link_token}`")
        await update.message.reply_text(f"✅  *لینک ثبت شد:*\n`{DOMAIN_URL}/{link_token}`\n\nمرحله بعد:", reply_markup=kb_old_next_or_finish(), parse_mode='Markdown')
        return OLD_ASK_NEXT_ACTION
    await wait_msg.edit_text("⚠️ کد نامعتبر است."); return OLD_ASK_CODE

async def old_resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("ارسال مجدد..."); await asyncio.to_thread(send_food_code, context.user_data.get('phone_number'))
    return OLD_ASK_CODE

async def old_next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text(f"📱 شماره اکانت قدیمی بعدی را وارد کنید:", reply_markup=kb_cancel()); return OLD_ASK_PHONE

async def old_finish_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phones = context.user_data.get('old_session_phones', [])
    context.user_data.clear()
    await update.callback_query.edit_message_text(f"📦 *لینک‌های صادر شده*\n\n" + "\n\n".join(phones), parse_mode='Markdown')
    return ConversationHandler.END

# --- چرخه اکانت‌های خام ---
async def next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text(f"📱 شماره مشتری بعدی را وارد کنید:", reply_markup=kb_cancel()); return ASK_PHONE

async def finish_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phones = context.user_data.get('session_phones', [])
    context.user_data.clear()
    await update.callback_query.edit_message_text(f"📦 *لینک‌های صادر شده*\n\n" + "\n\n".join(phones), parse_mode='Markdown')
    return ConversationHandler.END

async def start_batch_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("🗑 لطفاً تعداد خطوط قدیمی جهت حذف (از ته صف) را بفرستید:", reply_markup=kb_cancel()); return ASK_BATCH_DELETE_COUNT

async def process_batch_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    count = int(update.message.text.strip()) if update.message.text.strip().isdigit() else 0
    if count <= 0: return ASK_BATCH_DELETE_COUNT
    wait_msg = await update.message.reply_text("⏳ در حال حذف...")
    old_accs = [(k, json.loads(redis_client.get(k) or "{}").get("created_at", "")) for k in redis_client.keys("snappfood:license:*") if get_account_type(json.loads(redis_client.get(k) or "{}")) == "old"]
    old_accs.sort(key=lambda x: x[1])
    d_count = sum(1 for k, _ in old_accs[:count] if redis_client.delete(k))
    await wait_msg.edit_text(f"✅ {d_count} اکانت قدیمی حذف شد.", reply_markup=kb_admin_main())
    return ConversationHandler.END

# --- هندلرهای مربوط به بازسازی ---
async def start_rebuild(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "🔄 *بازسازی اتصال‌ها*\n\n"
        "تعداد اکانت‌هایی که می‌خواهید بازسازی شوند را وارد کنید:\n"
        "_(از جدیدترین اکانت‌ها به سمت قدیمی‌ها انجام می‌شود)_",
        reply_markup=kb_cancel(), parse_mode='Markdown'
    )
    return ASK_REBUILD_COUNT

async def process_rebuild_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("⚠️ لطفاً فقط یک عدد معتبر وارد کنید:", reply_markup=kb_cancel())
        return ASK_REBUILD_COUNT
    count = int(text)
    if count <= 0:
        await update.message.reply_text("⚠️ تعداد باید بیشتر از صفر باشد:", reply_markup=kb_cancel())
        return ASK_REBUILD_COUNT
        
    await update.message.reply_text("⏳ در حال پردازش و شروع بازسازی در پس‌زمینه...", parse_mode='Markdown')
    asyncio.ensure_future(process_database_rebuild(update.message.chat_id, context.bot, count))
    return ConversationHandler.END

# --- هندلرهای تعداد دستی برای چکرها ---
async def start_custom_checker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')
    context.user_data['checker_action'] = parts[2]
    context.user_data['checker_acc_type'] = parts[3]
    
    title = "تخفیف" if parts[2] == "discount" else "سابقه خرید"
    await query.edit_message_text(
        f"🔢 *بررسی تعداد دلخواه - چکر {title}*\n\n"
        "لطفاً تعداد اکانت‌هایی که می‌خواهید بررسی شوند را وارد کنید:\n"
        "_(از جدیدترین اکانت‌ها به سمت قدیمی‌ها انتخاب می‌شوند)_",
        reply_markup=kb_cancel(), parse_mode='Markdown'
    )
    return ASK_CHECKER_COUNT

async def process_custom_checker_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ لطفاً فقط یک عدد معتبر و بزرگتر از صفر وارد کنید:", reply_markup=kb_cancel())
        return ASK_CHECKER_COUNT
        
    count = int(text)
    action = context.user_data.get('checker_action')
    acc_type = context.user_data.get('checker_acc_type')
    
    await update.message.reply_text(f"⏳ در حال پردازش {count} اکانت اخیر در پس‌زمینه...", parse_mode='Markdown')
    
    if action == "discount":
        asyncio.ensure_future(process_discount_check(update.message.chat_id, context.bot, acc_type, mode="custom", count=count))
    elif action == "purchase":
        asyncio.ensure_future(process_purchase_check(update.message.chat_id, context.bot, acc_type, mode="custom", count=count))
        
    return ConversationHandler.END

# --- هندلرهای تنظیمات چکر خودکار ---
async def start_auto_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "⏳ *تنظیم زمان چکر خودکار*\n\n"
        "لطفاً فاصله زمانی بین هر دور بررسی را به **ساعت** وارد کنید:\n"
        "_(مثلاً وارد کنید `24` برای روزی یک‌بار)_",
        reply_markup=kb_cancel(), parse_mode='Markdown'
    )
    return ASK_AUTO_INTERVAL

async def process_auto_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ لطفاً فقط یک عدد صحیح بزرگتر از صفر وارد کنید:", reply_markup=kb_cancel())
        return ASK_AUTO_INTERVAL
        
    hours = int(text)
    if redis_client:
        config_raw = redis_client.get("config:auto_discount")
        config = json.loads(config_raw) if config_raw else {"enabled": False, "interval": 24}
        config["interval"] = hours
        redis_client.set("config:auto_discount", json.dumps(config))
        
        await update.message.reply_text(
            f"✅ زمان چکر خودکار روی `{hours}` ساعت تنظیم شد.",
            reply_markup=kb_auto_checker_menu(config), parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ دیتابیس متصل نیست.", reply_markup=kb_admin_main())
        
    return ConversationHandler.END

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USER_IDS: return await query.answer("⛔️ دسترسی ندارید.")
    data = query.data

    if data == 'admin_open' or data == 'admin_back':
        stats = get_database_account_stats()
        await query.edit_message_text(f"⚙️  *پنل مدیریت*\n\n📊  مجموع لینک‌ها: `{stats['total']}`\n🟠  خام: `{stats['raw']}` | 🔵  قدیمی: `{stats['old']}`", reply_markup=kb_admin_main(), parse_mode='Markdown')
    elif data in ['admin_get_list_raw', 'admin_get_list_old']:
        acc_type = "raw" if data == 'admin_get_list_raw' else "old"
        accounts = sorted([json.loads(redis_client.get(k) or "{}") | {"_k": k} for k in redis_client.keys("snappfood:license:*") if get_account_type(json.loads(redis_client.get(k) or "{}")) == acc_type], key=lambda x: x.get("created_at", ""))
        chunks = [accounts[i:i + 20] for i in range(0, len(accounts), 20)]
        await query.edit_message_text(f"⏳ درحال آماده‌سازی...")
        for idx, chunk in enumerate(chunks, 1):
            msg = f"📦 <b>دسته {idx}</b>\n" + "\n".join([f"{i}. {c.get('phone_number')}" for i, c in enumerate(chunk, 1)])
            msg += "\n\n<code>" + "\n".join([f"{DOMAIN_URL}/{c.get('link_token', c.get('_k').split(':')[-1])}" for c in chunk]) + "</code>"
            await context.bot.send_message(query.message.chat_id, msg, parse_mode='HTML')
            await asyncio.sleep(0.5)
        await context.bot.send_message(query.message.chat_id, "✅ ارسال تمام شد.", reply_markup=kb_admin_main())
    elif data == 'admin_stats':
        stats = get_database_account_stats()
        await query.edit_message_text(f"📊 *آمار سیستم*\nکل: `{stats['total']}` | خام: `{stats['raw']}` | قدیمی: `{stats['old']}`", reply_markup=kb_back_to_admin(), parse_mode='Markdown')
    elif data.startswith('admin_checkmenu_'):
        action = data.split('_')[2]
        acc_type = data.split('_')[3]
        title = "تخفیف" if action == "discount" else "سابقه خرید"
        await query.edit_message_text(f"❓ *چکر {title}*\nمایلید کدام دسته بررسی شود؟", reply_markup=kb_check_options(action, acc_type), parse_mode='Markdown')
    elif data.startswith('admin_run_'):
        parts = data.split('_')
        action = parts[2]
        mode = parts[3]
        acc_type = parts[4]
        await query.edit_message_text(f"🚀 چکر در پس‌زمینه استارت خورد...")
        if action == "discount": asyncio.ensure_future(process_discount_check(query.message.chat_id, context.bot, acc_type, mode))
        elif action == "purchase": asyncio.ensure_future(process_purchase_check(query.message.chat_id, context.bot, acc_type, mode))
    elif data == 'admin_autocheck_menu':
        config_raw = redis_client.get("config:auto_discount") if redis_client else None
        config = json.loads(config_raw) if config_raw else {"enabled": False, "interval": 24}
        await query.edit_message_text("🤖 *تنظیمات چکر خودکار تخفیف*\n\nدر این بخش می‌توانید ربات را تنظیم کنید تا در پس‌زمینه و با سرعت بسیار پایین (۳۰ الی ۶۰ ثانیه مکث برای هر خط)، بررسی را مدام انجام دهد و به محض یافتن تخفیف به شما پیام دهد.", reply_markup=kb_auto_checker_menu(config), parse_mode='Markdown')
    elif data == 'admin_autocheck_toggle':
        config_raw = redis_client.get("config:auto_discount") if redis_client else None
        config = json.loads(config_raw) if config_raw else {"enabled": False, "interval": 24}
        config["enabled"] = not config["enabled"]
        if redis_client:
            redis_client.set("config:auto_discount", json.dumps(config))
        await query.edit_message_reply_markup(reply_markup=kb_auto_checker_menu(config))
    elif data == 'admin_delete_hint':
        await query.message.reply_text("🗑 برای حذف، دستور زیر را بفرستید:\n`/delete BARANLINK-R-XXXX...`", parse_mode='Markdown')
    elif data == 'admin_extract' or data == 'admin_extract_tokens':
        lines = []
        for k in redis_client.keys("snappfood:license:*"):
            r = json.loads(redis_client.get(k) or "{}")
            t = r.get('link_token', k.split(':')[-1])
            lines.append(f"Link: {DOMAIN_URL}/{t} | Phone: {r.get('phone_number')} | Access: {'OK' if r.get('access_token') else 'No'}")
        doc = io.BytesIO("\n".join(lines).encode('utf-8'))
        doc.name = "Backup.txt"
        await query.message.reply_document(doc, caption="📥 فایل پشتیبان سیستم")

# ======================== اجرای ربات و سرور ========================
async def run_bot():
    logger.info(f"🔍 وضعیت سیستم تلگرام در حال بررسی است...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    
    # 1. هندلر بازسازی
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_rebuild, pattern='^admin_rebuild_start$')],
        states={ASK_REBUILD_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_rebuild_count)]},
        fallbacks=[CommandHandler("cancel", cancel_action), CommandHandler("start", start), CallbackQueryHandler(exit_to_admin, pattern='^admin_open$|^admin_back$')]
    ))
    
    # 2. هندلر حذف گروهی
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_batch_delete, pattern='^batch_delete_old_start$')],
        states={ASK_BATCH_DELETE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_batch_delete)]},
        fallbacks=[CommandHandler("cancel", cancel_action), CommandHandler("start", start), CallbackQueryHandler(exit_to_admin, pattern='^admin_open$|^admin_back$')]
    ))
    
    # 3. هندلر اکانت قدیمی
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(old_license_start, pattern='^admin_old_license$')],
        states={
            OLD_ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, old_ask_phone)],
            OLD_ASK_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, old_ask_code),
                CallbackQueryHandler(old_resend_code_callback, pattern='^old_resend_code$')
            ],
            OLD_ASK_NEXT_ACTION: [
                CallbackQueryHandler(old_next_line_callback, pattern='^old_next_line$'),
                CallbackQueryHandler(old_finish_session_callback, pattern='^old_finish_session$')
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_action), CommandHandler("start", start), CallbackQueryHandler(exit_to_admin, pattern='^admin_open$|^admin_back$')]
    ))
    
    # 4. هندلر لینک جدید
    app.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_raw_license_callback, pattern='^admin_new_license$')
        ],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_CODE_STEP_1: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code_step_1),
                CallbackQueryHandler(resend_code_1_callback, pattern='^resend_code_1$')
            ],
            ASK_CODE_STEP_2: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code_step_2),
                CallbackQueryHandler(resend_code_2_callback, pattern='^resend_code_2$')
            ],
            ASK_NEXT_ACTION: [
                CallbackQueryHandler(next_line_callback, pattern='^next_line$'),
                CallbackQueryHandler(finish_session_callback, pattern='^finish_session$')
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_action), CommandHandler("start", start), CallbackQueryHandler(exit_to_admin, pattern='^admin_open$|^admin_back$')]
    ))
    
    # 5. هندلر بررسی تعداد دلخواه برای چکرها 
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_custom_checker, pattern='^admin_checkcustom_')],
        states={ASK_CHECKER_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_custom_checker_count)]},
        fallbacks=[CommandHandler("cancel", cancel_action), CommandHandler("start", start), CallbackQueryHandler(exit_to_admin, pattern='^admin_open$|^admin_back$')]
    ))

    # 6. هندلر تنظیم زمان چکر خودکار 
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_auto_interval, pattern='^admin_autocheck_setint$')],
        states={ASK_AUTO_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_auto_interval)]},
        fallbacks=[CommandHandler("cancel", cancel_action), CommandHandler("start", start), CallbackQueryHandler(exit_to_admin, pattern='^admin_open$|^admin_back$')]
    ))
    
    # 7. بقیه دکمه‌های پنل
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern="^admin_"))
    
    await app.initialize()
    await app.start()
    
    logger.info("🗑 پاک‌سازی تداخلات احتمالی تلگرام...")
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    logger.info("🤖 ارتباط برقرار شد.")
    asyncio.create_task(auto_discount_checker_loop(app.bot))
    
    await asyncio.Event().wait()

async def run_webserver():
    server = uvicorn.Server(uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="info"))
    await server.serve()

async def main():
    await asyncio.gather(run_bot(), run_webserver())

if __name__ == "__main__":
    asyncio.run(main())
