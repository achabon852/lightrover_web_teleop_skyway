from __future__ import annotations
import time
import uuid
import jwt


def create_skyway_token(
    *,
    app_id: str,
    secret_key: str,
    room_name: str,
    member_name: str,
    can_publish: bool,
    can_subscribe: bool,
    ttl_sec: int = 60 * 60,
) -> str:
    """Create a SkyWay Auth Token v3 using the same scoped format as the working app."""
    now = int(time.time())
    member_methods: list[str] = []
    if can_publish:
        member_methods.append("publish")
    if can_subscribe:
        member_methods.append("subscribe")

    payload = {
        "iat": now,
        "jti": str(uuid.uuid4()),
        "exp": now + min(ttl_sec, 60 * 60 * 24 * 3),
        "version": 3,
        "scope": {
            "appId": app_id,
            "turn": {"enabled": True},
            "rooms": [
                {
                    "id": "*",
                    "name": room_name,
                    "methods": ["create", "close", "updateMetadata"],
                    "sfu": {"enabled": True, "maxSubscribersLimit": 8},
                    "member": {
                        "id": "*",
                        "name": member_name,
                        "methods": member_methods,
                    },
                }
            ],
        },
    }
    return jwt.encode(payload, secret_key, algorithm="HS256", headers={"typ": "JWT"})
