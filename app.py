from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import requests
import json
import os
import base64
import time
import like_pb2
import like_count_pb2
import uid_generator_pb2
import threading
import urllib3
from datetime import datetime, timezone

# -------------------------------
# API KEY CONFIGURATION
# -------------------------------
API_KEY = "hkysxbhyws"
WRONG_KEY_RESPONSE = {
    "error": "Unauthorized",
    "credits": "@HAFSAN_KUN"
}
# -------------------------------

LIKES_PER_TOKEN_PER_DAY = 30
PROFILE_CHECK_TOKEN_COUNT = 15          # প্রথম ১৫টি টোকেন = প্রোফাইল চেক
ALLOW_PROFILE_TOKENS_AS_FALLBACK = True # True হলে লাইক-টোকেন শেষ হলে প্রথম ১৫টাও লাইকে যাবে
TOKEN_FILE = "token_bd.json"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -------------------------------
# STATE (Vercel stateless → /tmp)
# -------------------------------
STATE_FILE = "/tmp/token_usage_state.json"
state_lock = threading.Lock()


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    default = {"date": _today_utc(), "usage": {}}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        if state.get("date") != _today_utc():
            print("New UTC day -> resetting limits")
            return default
        state.setdefault("usage", {})
        return state
    except Exception as e:
        print(f"State load error: {e}")
        return default


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"State save error: {e}")


def load_token_file(file_name):
    try:
        with open(file_name, "r") as f:
            tokens = json.load(f)
        result = []
        if isinstance(tokens, list):
            for t in tokens:
                if isinstance(t, dict) and "token" in t:
                    result.append(t)
                elif isinstance(t, str):
                    result.append({"token": t})
        if result:
            return result
        print(f"Warning: {file_name} invalid format.")
        return []
    except FileNotFoundError:
        print(f"Warning: {file_name} not found.")
        return []
    except json.JSONDecodeError:
        print(f"Warning: {file_name} invalid JSON.")
        return []


def load_all_tokens():
    tokens = load_token_file(TOKEN_FILE)
    print(f"Loaded {len(tokens)} tokens from {TOKEN_FILE}")
    return tokens


# -------------------------------
# JWT HELPERS (টোকেনের ভেতরের আইডি পড়া + এক্সপায়ার চেক)
# -------------------------------
def decode_jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def is_token_alive(token: str) -> bool:
    data = decode_jwt_payload(token)
    exp = data.get("exp", 0)
    return exp == 0 or exp > time.time()


def token_account_id(token: str):
    return decode_jwt_payload(token).get("account_id")


# -------------------------------
# CRYPTO / PROTOBUF
# -------------------------------
def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded)).decode('utf-8')


def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()


def create_protobuf_for_profile_check(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return message.SerializeToString()


def enc_profile_check_payload(uid):
    return encrypt_message(create_protobuf_for_profile_check(uid))


# -------------------------------
# HTTP CALLS
# -------------------------------
async def send_single_like_request(encrypted_like_payload, token_dict, url):
    edata = bytes.fromhex(encrypted_like_payload)
    token_value = token_dict.get("token", "")
    if not token_value:
        return 999
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as response:
                return response.status
    except asyncio.TimeoutError:
        return 998
    except Exception:
        return 997


def make_profile_check_request(encrypted_profile_payload, server_name, token_dict):
    token_value = token_dict.get("token", "")
    if not token_value:
        return None
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ppmainecoonghj.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_profile_payload)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        return decode_protobuf_profile_info(response.content)
    except Exception as e:
        print(f"Profile check error: {e}")
        return None


def decode_protobuf_profile_info(binary_data):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary_data)
        return items
    except Exception as e:
        print(f"Decode error: {e}")
        return None


def get_like_api_url(server_name):
    if server_name == "IND":
        return "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/LikeProfile"
    else:
        return "https://clientbp.ppmainecoonghj.com/LikeProfile"


# -------------------------------
# TOKEN SELECTION (নতুন লজিক)
# -------------------------------
def split_tokens(all_tokens):
    """
    প্রথম PROFILE_CHECK_TOKEN_COUNT টি = প্রোফাইল চেক টোকেন
    বাকিগুলো = লাইক টোকেন
    এক্সপায়ার হওয়া টোকেন বাদ দেওয়া হয়।
    """
    alive = [t for t in all_tokens if is_token_alive(t["token"])]
    profile_tokens = alive[:PROFILE_CHECK_TOKEN_COUNT]
    like_tokens = alive[PROFILE_CHECK_TOKEN_COUNT:]
    return profile_tokens, like_tokens


def get_next_profile_token(all_tokens, state):
    """প্রোফাইল চেকের জন্য প্রথম ১৫টির মধ্যে থেকে একটি রাউন্ড-রবিন বেছে নেয়।"""
    profile_tokens, like_tokens = split_tokens(all_tokens)
    pool = profile_tokens if profile_tokens else like_tokens
    if not pool:
        return None
    usage = state["usage"]
    return min(pool, key=lambda t: usage.get(t["token"], 0))


def select_tokens_for_likes(all_tokens, state):
    """
    লাইক পাঠানোর জন্য টোকেন বেছে নেয়।
    1) আগে শুধু লাইক-টোকেন (index >= 15) থেকে
    2) যদি সেগুলোর লিমিট শেষ হয় এবং ALLOW_PROFILE_TOKENS_AS_FALLBACK=True,
       তাহলে প্রথম ১৫টাও ব্যবহার করে।
    """
    profile_tokens, like_tokens = split_tokens(all_tokens)
    usage = state["usage"]

    def available(tokens):
        out = []
        for td in tokens:
            tok = td["token"]
            if usage.get(tok, 0) < LIKES_PER_TOKEN_PER_DAY:
                out.append((td, usage.get(tok, 0)))
        return out

    selected = available(like_tokens)
    if not selected and ALLOW_PROFILE_TOKENS_AS_FALLBACK:
        selected = available(profile_tokens)
    return selected


def usage_summary(all_tokens, state):
    """হোম/টোকেন_ইনফোতে দেখানোর জন্য সারাংশ।"""
    profile_tokens, like_tokens = split_tokens(all_tokens)
    usage = state["usage"]

    def used_count(tokens):
        return sum(usage.get(t["token"], 0) for t in tokens)

    p_used = used_count(profile_tokens)
    l_used = used_count(like_tokens)
    p_cap = len(profile_tokens) * LIKES_PER_TOKEN_PER_DAY
    l_cap = len(like_tokens) * LIKES_PER_TOKEN_PER_DAY
    return {
        "profile_check_tokens": len(profile_tokens),
        "like_tokens": len(like_tokens),
        "profile_used": p_used,
        "profile_capacity": p_cap,
        "like_used": l_used,
        "like_capacity": l_cap,
        "total_used": p_used + l_used,
        "total_capacity": p_cap + l_cap,
    }


# -------------------------------
# FLASK APP
# -------------------------------
app = Flask(__name__)


@app.route('/', methods=['GET'])
def home():
    all_tokens = load_all_tokens()
    with state_lock:
        state = load_state()
    s = usage_summary(all_tokens, state)

    return jsonify({
        "api": "Free Fire Like API",
        "status": "online",
        "endpoints": {
            "/like": "?key=YOUR_KEY&uid=UID&server_name=BD",
            "/token_info": "?key=YOUR_KEY"
        },
        "config": {
            "token_file": TOKEN_FILE,
            "total_tokens_loaded": len(all_tokens),
            "profile_check_token_count": PROFILE_CHECK_TOKEN_COUNT,
            "likes_per_token_per_day": LIKES_PER_TOKEN_PER_DAY,
            "fallback_enabled": ALLOW_PROFILE_TOKENS_AS_FALLBACK
        },
        "today_usage": {
            "date_utc": state.get("date"),
            **s,
            "remaining": max(0, s["total_capacity"] - s["total_used"]),
        },
        "credits": "@HAFSAN_KUN"
    })


@app.route('/like', methods=['GET'])
def handle_requests():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    uid_param = request.args.get("uid")
    server_name_param = request.args.get("server_name", "BD").upper()

    if not uid_param:
        return jsonify({"error": "UID is required", "credits": "@HAFSAN_KUN"}), 400

    all_tokens = load_all_tokens()
    if not all_tokens:
        return jsonify({
            "error": f"No tokens loaded from {TOKEN_FILE}",
            "credits": "@HAFSAN_KUN"
        }), 500

    with state_lock:
        state = load_state()

    profile_token = get_next_profile_token(all_tokens, state)
    if not profile_token:
        return jsonify({"error": "No alive token for profile check."}), 500

    # before snapshot
    enc_uid = enc_profile_check_payload(uid_param)
    before_info = make_profile_check_request(enc_uid, server_name_param, profile_token)
    before_likes = int(before_info.AccountInfo.Likes) if (before_info and hasattr(before_info, 'AccountInfo')) else 0
    print(f"UID {uid_param}: before = {before_likes}")

    with state_lock:
        state = load_state()
        selected = select_tokens_for_likes(all_tokens, state)

    if not selected:
        return jsonify({
            "LikesGivenByAPI": 0,
            "LikesafterCommand": before_likes,
            "LikesbeforeCommand": before_likes,
            "PlayerNickname": "N/A",
            "UID": int(uid_param),
            "status": 2,
            "message": f"All tokens reached daily limit ({LIKES_PER_TOKEN_PER_DAY}/token/day).",
            "credits": "@HAFSAN_KUN"
        })

    like_proto = create_protobuf_message(uid_param, server_name_param)
    enc_like_payload = encrypt_message(like_proto)
    like_url = get_like_api_url(server_name_param)

    async def send_all():
        tasks = [
            send_single_like_request(enc_like_payload, td, like_url)
            for (td, _u) in selected
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(send_all())
    finally:
        loop.close()

    success_count = 0
    with state_lock:
        state = load_state()
        usage = state["usage"]
        for (td, _u), res in zip(selected, results):
            if isinstance(res, int) and res == 200:
                tok = td["token"]
                usage[tok] = usage.get(tok, 0) + 1
                success_count += 1
        save_state(state)

    # after snapshot
    after_info = make_profile_check_request(enc_uid, server_name_param, profile_token)
    after_likes = int(after_info.AccountInfo.Likes) if (after_info and hasattr(after_info, 'AccountInfo')) else before_likes

    nickname = "N/A"
    if after_info and hasattr(after_info, 'AccountInfo') and after_info.AccountInfo.PlayerNickname:
        nickname = str(after_info.AccountInfo.PlayerNickname)

    # ✅ লাইক রেজাল্ট এখন success_count ভিত্তিক (আগের before/after তুলনা ভুল ছিল)
    status = 1 if success_count > 0 else 2

    with state_lock:
        state = load_state()
    s = usage_summary(all_tokens, state)

    return jsonify({
        "LikesGivenByAPI": success_count,
        "LikesafterCommand": after_likes,
        "LikesbeforeCommand": before_likes,
        "PlayerNickname": nickname,
        "UID": int(uid_param),                 # ✅ ইউজারের দেওয়া UID ফেরত
        "status": status,
        "ProfileCheckedWith": token_account_id(profile_token["token"]),
        "TokensUsedThisRequest": len(selected),
        "SuccessfulSends": success_count,
        "DailyUsage": {
            **s,
            "remaining": max(0, s["total_capacity"] - s["total_used"]),
        },
        "credits": "@HAFSAN_KUN"
    })


@app.route('/token_info', methods=['GET'])
def token_info():
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify(WRONG_KEY_RESPONSE), 401

    all_tokens = load_all_tokens()
    with state_lock:
        state = load_state()
        usage = state["usage"]

    profile_tokens, like_tokens = split_tokens(all_tokens)
    s = usage_summary(all_tokens, state)

    def describe(tokens, role):
        rows = []
        for t in tokens:
            tok = t["token"]
            u = usage.get(tok, 0)
            rows.append({
                "account_id": token_account_id(tok),
                "role": role,
                "token_preview": tok[:8] + "..." if len(tok) > 8 else tok,
                "used": u,
                "remaining": max(0, LIKES_PER_TOKEN_PER_DAY - u),
                "exhausted": u >= LIKES_PER_TOKEN_PER_DAY,
                "alive": is_token_alive(tok),
            })
        return rows

    return jsonify({
        "date_utc": state.get("date"),
        "file": TOKEN_FILE,
        "config": {
            "LIKES_PER_TOKEN_PER_DAY": LIKES_PER_TOKEN_PER_DAY,
            "PROFILE_CHECK_TOKEN_COUNT": PROFILE_CHECK_TOKEN_COUNT,
            "ALLOW_PROFILE_TOKENS_AS_FALLBACK": ALLOW_PROFILE_TOKENS_AS_FALLBACK,
        },
        "summary": {
            **s,
            "remaining": max(0, s["total_capacity"] - s["total_used"]),
        },
        "profile_check_tokens": describe(profile_tokens, "profile_check"),
        "like_tokens": describe(like_tokens, "like"),
        "credits": "@HAFSAN_KUN"
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)