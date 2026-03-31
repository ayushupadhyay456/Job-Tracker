import os
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport import requests as grequests

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def get_flow(redirect_uri: str) -> Flow:
    client_config = {
        "web": {
            "client_id":     os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    return flow


def get_user_info(credentials) -> dict:
    request = grequests.Request()
    id_info = id_token.verify_oauth2_token(
        credentials.id_token, request, os.environ["GOOGLE_CLIENT_ID"]
    )
    return {
        "google_id": id_info["sub"],
        "email":     id_info.get("email", ""),
        "name":      id_info.get("name", ""),
        "picture":   id_info.get("picture", ""),
    }