"""OAuth resource server for one approved finance snapshot; no login or token issuance."""
import asyncio
import json
import os
from urllib.parse import urlsplit

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .contract import ContractError

SCOPE = "finance:read"


def https_url(value: str, *, endpoint: bool = False) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or (endpoint and parts.path != "/mcp")):
        raise ContractError("Нужен HTTPS URL без ключей/параметров; MCP endpoint должен иметь путь /mcp")
    try:
        parts.port
    except ValueError:
        raise ContractError("Некорректный порт URL") from None
    return value


class OAuthJWTVerifier:
    """Only configured RS256 keys/issuer/audience, approved subjects and read scope are trusted."""

    def __init__(self, issuer: str, resource: str, jwks_url: str, subjects: tuple[str, ...]):
        import jwt
        self.issuer = https_url(issuer)
        self.resource = https_url(resource, endpoint=True)
        self.jwks_url = https_url(jwks_url)
        if not subjects or any(not isinstance(s, str) or not s.strip() for s in subjects):
            raise ContractError("Нужен явный список разрешённых OAuth sub владельцев этого снимка")
        self.subjects = frozenset(subjects)
        self.keys = jwt.PyJWKClient(self.jwks_url, cache_jwk_set=True, lifespan=300, timeout=5)

    def _verify(self, token: str):
        import jwt
        try:
            # Never trust jku/x5u URLs or algorithms supplied by a caller.
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.resource,
                                issuer=self.issuer, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            sub = claims["sub"]
            if not isinstance(sub, str) or sub not in self.subjects:
                return None
            scope = claims.get("scope", "")
            if not isinstance(scope, str) or SCOPE not in scope.split():
                return None
            client = claims.get("client_id") or claims.get("azp")
            if not isinstance(client, str) or not client.strip():
                return None
            return AccessToken(token=token, client_id=client, scopes=scope.split(),
                               expires_at=int(claims["exp"]), resource=self.resource, subject=sub)
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            # No raw token, keys, provider response or financial values in error messages.
            return None

    async def verify_token(self, token: str):
        if not token or len(token) > 16384:
            return None
        return await asyncio.to_thread(self._verify, token)


def create_http_server(adapter, *, environ=None):
    from .server import create_server
    env = os.environ if environ is None else environ
    names = ["FINANCE_MCP_PUBLIC_URL", "FINANCE_MCP_OAUTH_ISSUER",
             "FINANCE_MCP_JWKS_URL", "FINANCE_MCP_ALLOWED_SUBJECTS"]
    if any(not env.get(name) for name in names):
        raise ContractError("Для HTTP задайте PUBLIC_URL, OAUTH_ISSUER, JWKS_URL и ALLOWED_SUBJECTS с префиксом FINANCE_MCP_")
    try:
        subjects = json.loads(env["FINANCE_MCP_ALLOWED_SUBJECTS"])
    except (ValueError, TypeError):
        raise ContractError("ALLOWED_SUBJECTS должен быть непустым JSON-массивом строк") from None
    if not isinstance(subjects, list):
        raise ContractError("ALLOWED_SUBJECTS должен быть JSON-массивом")
    verifier = OAuthJWTVerifier(env[names[1]], env[names[0]], env[names[2]], tuple(subjects))
    server = create_server(adapter, token_verifier=verifier, auth=AuthSettings(
        issuer_url=AnyHttpUrl(verifier.issuer), resource_server_url=AnyHttpUrl(verifier.resource),
        required_scopes=[SCOPE], validate_token_resource=True,
        service_documentation_url=AnyHttpUrl("https://finuchetai.ru/")))
    parts = urlsplit(verifier.resource)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=[parts.netloc], allowed_origins=["https://" + parts.netloc])
    return server, security


def create_http_app(adapter, *, environ=None):
    server, security = create_http_server(adapter, environ=environ)
    return server.streamable_http_app(stateless_http=True, json_response=True,
        transport_security=security, max_request_body_size=65536)


def serve_http(adapter, *, host: str, port: int):
    import uvicorn
    if not 1 <= port <= 65535:
        raise ContractError("HTTP порт должен быть от 1 до 65535")
    app = create_http_app(adapter)
    # TLS terminates at the approved reverse proxy; no auth-free remote mode.
    uvicorn.run(app, host=host, port=port, access_log=False, log_level="warning")
