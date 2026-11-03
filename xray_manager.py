import json
import os
import subprocess
import asyncio
import logging

XRAY_CONFIG_PATH = "/usr/local/bin/config.json"
XRAY_BIN = "/usr/local/bin/xray"

xray_process = None

def generate_xray_config(inbounds_data):
    clients_vless = []
    clients_vmess = []

    for ib in inbounds_data:
        if not ib.get("enabled", True):
            continue
            
        uuid = ib["uuid"]
        uid = ib["uid"]
        # Use uid as the email so stats can be mapped back to inbound by uid
        clients_vless.append({"id": uuid, "email": uid})
        clients_vmess.append({"id": uuid, "email": uid})

    config = {
        "log": {"loglevel": "warning"},
        "dns": {
            "servers": [
                "https+local://1.1.1.1/dns-query",
                "https+local://8.8.8.8/dns-query",
                "1.1.1.1",
                "8.8.8.8",
                "localhost"
            ],
            "queryStrategy": "UseIPv4"
        },
        "api": {
            "tag": "api",
            "services": ["StatsService"]
        },
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True
            }
        },
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
                "tag": "api"
            },
            {
                "listen": "127.0.0.1",
                "port": 10001,
                "protocol": "vless",
                "settings": {"clients": clients_vless, "decryption": "none"},
                "streamSettings": {"network": "ws", "wsSettings": {"path": "/vl-ws"}},
                "tag": "inbound-vless-ws"
            },
            {
                "listen": "127.0.0.1",
                "port": 10002,
                "protocol": "vmess",
                "settings": {"clients": clients_vmess},
                "streamSettings": {"network": "ws", "wsSettings": {"path": "/vm-ws"}},
                "tag": "inbound-vmess-ws"
            },
            {
                "listen": "127.0.0.1",
                "port": 10004,
                "protocol": "vless",
                "settings": {"clients": clients_vless, "decryption": "none"},
                "streamSettings": {"network": "xhttp", "xhttpSettings": {"path": "/vl-xhttp"}},
                "tag": "inbound-vless-xhttp"
            }
        ],
        "outbounds": [{"protocol": "freedom"}],
        "routing": {
            "rules": [
                {
                    "inboundTag": ["api"],
                    "outboundTag": "api",
                    "type": "field"
                }
            ]
        }
    }

    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

def restart_xray():
    global xray_process
    if xray_process and xray_process.poll() is None:
        xray_process.terminate()
        try:
            xray_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xray_process.kill()
    
    # Start Xray
    if os.path.exists(XRAY_BIN):
        xray_process = subprocess.Popen([XRAY_BIN, "run", "-c", XRAY_CONFIG_PATH])
    else:
        logging.warning("Xray binary not found. Running in mock/dev mode.")

previous_stats = {}

async def get_xray_stats():
    """Query Xray stats API and return per-uid traffic deltas.
    
    Stats are keyed by uid (the email field set in generate_xray_config),
    so they can be directly matched to inbounds via inbound_by_uid().
    """
    global previous_stats
    if not os.path.exists(XRAY_BIN):
        return {}
        
    try:
        proc = await asyncio.create_subprocess_exec(
            XRAY_BIN, "api", "statsquery", "--server=127.0.0.1:10085",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        out = stdout.decode("utf-8")
        
        deltas = {}
        import re
        matches = re.findall(r'name:\s*"([^"]+)"\s*value:\s*(\d+)', out)
        
        current_stats = {}
        for name, value in matches:
            parts = name.split(">>>")
            if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
                uid = parts[1]  # This is the email field = inbound uid
                direction = parts[3] # uplink or downlink
                val = int(value)
                
                if uid not in current_stats:
                    current_stats[uid] = {"up": 0, "down": 0}
                if direction == "uplink":
                    current_stats[uid]["up"] += val
                elif direction == "downlink":
                    current_stats[uid]["down"] += val

        for uid, stats in current_stats.items():
            prev = previous_stats.get(uid, {"up": 0, "down": 0})
            up_delta = stats["up"] - prev["up"]
            down_delta = stats["down"] - prev["down"]
            
            # If Xray restarted, value might be less than prev, just take current as delta
            if up_delta < 0: up_delta = stats["up"]
            if down_delta < 0: down_delta = stats["down"]
            
            if up_delta > 0 or down_delta > 0:
                deltas[uid] = {"up": up_delta, "down": down_delta}
                
        previous_stats = current_stats
        return deltas
    except Exception as e:
        logging.error(f"Error querying Xray stats: {e}")
        return {}
