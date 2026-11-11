"""
Protótipo Palm X — backend (FastAPI)

Responsabilidades:
  1. Guardar a api_key (ak_...) da Tencent Palm X — ela NUNCA vai para o navegador/app.
  2. Emitir tokens de usuário de curta duração (sut_...) para o SDK (CreateSdkUserToken).
  3. Confirmar no servidor o resultado devolvido pelo SDK (GetSessionResult).
  4. Consultar e excluir palmas cadastradas (GetPalm / DeletePalm).
  5. Registrar o consentimento do titular antes do cadastro (LGPD).

Referência da API: https://palm.tencent.com/docs/kyc-api
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PALM_BASE_URL = os.getenv("PALM_BASE_URL", "https://open.intl.palm.tencent.com").rstrip("/")
PALM_APP_ID = os.getenv("PALM_APP_ID", "")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "600"))
CONSENT_LOG = Path(os.getenv("CONSENT_LOG", str(ROOT / "consent_log.jsonl")))
EVENTS_LOG = Path(os.getenv("EVENTS_LOG", str(ROOT / "events.jsonl")))
CONSENT_VERSION = "v2-2026-11"

# Caminhos documentados em https://palm.tencent.com/docs/kyc-api
PATH_CREATE_TOKEN = "/palm/openai/x/oauth/sdk/usertoken/create"
PATH_GET_PALM = "/palm/openai/get_palm"
PATH_GET_SESSION = "/palm/openai/get_session_result"
PATH_DELETE_PALM = "/palm/openai/delete_palm"

CODE_NO_PALM = 2080009  # GetPalm: nenhuma palma cadastrada
CODE_USER_NOT_IN_DB = 2080016  # usuário não existe

USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DIRECTIONS = {"left": 1, "right": 2}  # PalmDirectionType
DIRECTION_NAMES = {1: "left", 2: "right"}

# Sobrescrito nos testes para simular a API da Tencent.
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

app = FastAPI(title="Protótipo Palm X", version="0.1.0")


class PalmApiError(Exception):
    def __init__(self, code: int, message: str, request_id: str = ""):
        self.code, self.message, self.request_id = code, message, request_id


@app.exception_handler(PalmApiError)
async def palm_error_handler(_: Request, exc: PalmApiError):
    return JSONResponse(
        status_code=502,
        content={
            "erro": "A API Palm X recusou a operação.",
            "codigo": exc.code,
            "mensagem": exc.message,
            "requestId": exc.request_id,
        },
    )


def _api_key() -> str:
    key = os.getenv("PALM_API_KEY", "")
    if not key:
        raise HTTPException(503, "PALM_API_KEY não configurada no servidor (.env).")
    return key


async def palm_call(path: str, body: dict) -> dict:
    """POST na API Palm X. Devolve o campo `data`; lança PalmApiError se code != 0.

    A API responde HTTP 200 mesmo em falha de negócio/autenticação: o que vale
    é o `code` do corpo.
    """
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {_api_key()}",
        "X-TraceId": uuid.uuid4().hex,
    }
    try:
        async with httpx.AsyncClient(base_url=PALM_BASE_URL, timeout=15, transport=TRANSPORT) as client:
            resp = await client.post(path, json=body, headers=headers)
            resp.raise_for_status()
            envelope = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Falha de comunicação com a API Palm X: {exc}") from exc

    if envelope.get("code") != 0:
        raise PalmApiError(
            int(envelope.get("code", -1)),
            str(envelope.get("message", "")),
            str(envelope.get("requestId", "")),
        )
    return envelope.get("data") or {}


def check_user_id(user_id: str) -> str:
    if not USER_ID_RE.match(user_id):
        raise HTTPException(422, "userId inválido: use 1 a 64 caracteres entre letras, dígitos, '-' e '_'.")
    return user_id


def log_consent(user_id: str, request: Request) -> None:
    record = {
        "userId": user_id,
        "consentVersion": CONSENT_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ip": request.client.host if request.client else None,
        "userAgent": request.headers.get("user-agent"),
    }
    with CONSENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- modelos ----------

class TokenRequest(BaseModel):
    userId: str
    purpose: Literal["registration", "verification"]
    consent: bool = False


class ConfirmRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=128)
    userId: Optional[str] = None  # dispensado em "recognition" (1:N)
    purpose: Literal["registration", "verification", "recognition"]


class DeleteRequest(BaseModel):
    directions: list[Literal["left", "right"]] = Field(default_factory=lambda: ["left", "right"])


def _palm_info(info: Optional[dict]) -> Optional[dict]:
    if not info:
        return None
    return {
        "ativa": info.get("PalmState") == 1,          # 1 = ACTIVATED
        "altaSimilaridade": info.get("PalmState") == 2,  # 2 = HIGH_SIMILARITY
        "registradaEm": info.get("RegisterTime"),     # Unix (s), UTC
    }


async def fetch_palms(user_id: str) -> dict:
    try:
        data = await palm_call(PATH_GET_PALM, {"UserId": user_id})
    except PalmApiError as exc:
        if exc.code in (CODE_NO_PALM, CODE_USER_NOT_IN_DB):
            data = {}
        else:
            raise
    return {
        "userId": user_id,
        "left": _palm_info(data.get("LeftPalm")),
        "right": _palm_info(data.get("RightPalm")),
    }


# ---------- rotas ----------

@app.get("/api/config")
async def config():
    return {
        "appId": PALM_APP_ID,
        "consentVersion": CONSENT_VERSION,
        "apiKeyConfigurada": bool(os.getenv("PALM_API_KEY")),
    }


@app.post("/api/token")
async def create_token(req: TokenRequest, request: Request):
    """Emite um sdk_user_token (sut_) vinculado ao userId, para o SDK usar."""
    user_id = check_user_id(req.userId)
    if req.purpose == "registration":
        if not req.consent:
            raise HTTPException(400, "O cadastro exige consentimento explícito do titular.")
        log_consent(user_id, request)

    data = await palm_call(
        PATH_CREATE_TOKEN,
        {"UserId": user_id, "TtlSeconds": TOKEN_TTL_SECONDS, "Scope": []},
    )
    return {"token": data.get("Token"), "expiresAt": data.get("ExpiresAt"), "appId": PALM_APP_ID}


@app.post("/api/session/confirm")
async def confirm_session(req: ConfirmRequest):
    """Confirma no servidor o resultado que o SDK devolveu ao navegador.

    O retorno do SDK no cliente não é confiável sozinho: o sessionId é resolvido
    aqui com a api_key (GetSessionResult só aceita api_key).
    """
    if req.purpose != "recognition" and not req.userId:
        raise HTTPException(422, "userId é obrigatório para cadastro e verificação 1:1.")

    data = await palm_call(PATH_GET_SESSION, {"SessionId": req.sessionId})
    search = data.get("SearchResult") or {}

    if req.purpose == "recognition":
        # 1:N — quem diz de quem é a palma é a plataforma, não o navegador.
        is_match = bool(search.get("IsMatch"))
        found = data.get("UserId") if is_match else None
        return {
            "purpose": req.purpose,
            "confirmado": bool(found),
            "userId": found,
            "isMatch": is_match,
            "score": search.get("Score"),
            "algoritmo": search.get("AlgorithmVersion"),
            "palma": DIRECTION_NAMES.get(data.get("PalmDirection")),
        }

    user_id = check_user_id(req.userId)
    same_user = data.get("UserId") == user_id
    out = {
        "purpose": req.purpose,
        "usuarioConfere": same_user,
        "palma": DIRECTION_NAMES.get(data.get("PalmDirection")),
    }
    if req.purpose == "verification":
        is_match = bool(search.get("IsMatch"))
        out.update(
            confirmado=same_user and is_match,
            isMatch=is_match,
            score=search.get("Score"),
            algoritmo=search.get("AlgorithmVersion"),
        )
    else:
        out["confirmado"] = same_user
        if same_user:
            out["palmas"] = await fetch_palms(user_id)
    return out


@app.get("/api/palm/{user_id}")
async def get_palm(user_id: str):
    return await fetch_palms(check_user_id(user_id))


@app.delete("/api/palm/{user_id}")
async def delete_palm(user_id: str, req: Optional[DeleteRequest] = None):
    """Exclui a(s) palma(s) do titular (direito de eliminação — LGPD). Idempotente."""
    user_id = check_user_id(user_id)
    directions = (req or DeleteRequest()).directions
    if not directions:
        raise HTTPException(422, "Informe ao menos uma direção: left e/ou right.")
    try:
        await palm_call(
            PATH_DELETE_PALM,
            {"UserId": user_id, "PalmDirectionList": sorted({DIRECTIONS[d] for d in directions})},
        )
    except PalmApiError as exc:
        if exc.code not in (CODE_USER_NOT_IN_DB, CODE_NO_PALM):
            raise
    return {"userId": user_id, "excluidas": directions}


class EventRequest(BaseModel):
    """Evento do funil de cadastro. De propósito, não carrega identificador do usuário."""
    event: str = Field(pattern=r"^[a-z_]{1,40}$")
    step: Optional[str] = Field(default=None, max_length=8)
    code: Optional[int] = None
    attempt: Optional[int] = Field(default=None, ge=0, le=99)
    reason: Optional[str] = Field(default=None, max_length=24)


@app.post("/api/event", status_code=204)
async def log_event(ev: EventRequest):
    """Grava métricas de uso (em que passo as pessoas param, quais erros aparecem)."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **ev.model_dump(exclude_none=True)}
    with EVENTS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")
