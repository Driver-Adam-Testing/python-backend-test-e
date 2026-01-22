"""
OAuth authentication configuration for MCP server using Auth0.
"""

import logging

from cryptography.fernet import Fernet
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from app.auth.models import User
from app.core.config import settings

logger = logging.getLogger(__name__)


class OAuthAuthenticationError(Exception):
    """Exception raised when OAuth authentication or authorization fails"""


def _create_storage_backend() -> MemoryStore | RedisStore | FernetEncryptionWrapper:
    backend = settings.MCP_STORAGE_BACKEND.lower()

    if backend == "redis":
        # Required for multi-node env
        # Also adds at-rest encryption for prod use
        logger.info(
            f"Using Redis storage backend: {settings.REDIS_HOST}:{settings.REDIS_PORT}"
        )
        store = RedisStore(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD,
            db=0,
        )

        fernet = Fernet(settings.mcp_fernet_key)
        return FernetEncryptionWrapper(key_value=store, fernet=fernet)

    elif backend == "memory":
        # Memory store used for local dev only
        logger.info("Using in-memory storage backend (not suitable for production)")
        return MemoryStore()

    else:
        raise ValueError(
            f"Invalid MCP_STORAGE_BACKEND: {backend}. Must be 'redis' or 'memory'"
        )


def create_mcp_oauth_provider() -> OIDCProxy:
    """
    Create OIDCProxy for MCP server OAuth authentication with Auth0.

    Uses OIDCProxy directly instead of Auth0Provider to allow separate configuration
    of authorization scopes (including offline_access for refresh tokens) vs token
    validation scopes. The offline_access scope is needed to obtain refresh tokens
    but is not included in the access token's scope claim per OAuth2 spec.

    Implementation based on: https://fastmcp.wiki/en/deployment/http#mounting-authenticated-servers

    URL Structure when mounted at /mcp:
    - Client connects to: {PUBLIC_BASE_URL}/mcp/v1
    - OAuth discovery (root level): {PUBLIC_BASE_URL}/.well-known/oauth-authorization-server
    - OAuth callbacks: {PUBLIC_BASE_URL}/mcp/auth/callback
    - MCP protocol: {PUBLIC_BASE_URL}/mcp/v1/*

    Discovery Flow (RFC 9470):
    1. Client tries /mcp/v1 -> gets 401 with resource_metadata URL
    2. Client fetches /.well-known/oauth-protected-resource/mcp/v1
    3. Client discovers OAuth server at /.well-known/oauth-authorization-server
    4. Client completes OAuth flow and retries /mcp/v1 with token

    Security Configuration:
    - JWT signing key: Signs FastMCP-issued tokens
    - Storage encryption: Encrypts OAuth tokens at rest using Fernet
    - Client storage: Persists OAuth state (memory for dev, Redis for prod)
    """
    config_url = f"https://{settings.AUTH0_DOMAIN}/.well-known/openid-configuration"
    mcp_full_url = f"{settings.PUBLIC_BASE_URL}/mcp"

    logger.info(f"Configuring MCP OAuth with Auth0 domain: {settings.AUTH0_DOMAIN}")
    logger.info(f"Public base URL: {settings.PUBLIC_BASE_URL}")
    logger.info(f"MCP mounted at: {mcp_full_url}")
    logger.info(f"Storage backend: {settings.MCP_STORAGE_BACKEND}")

    storage = _create_storage_backend()

    auth = OIDCProxy(
        config_url=config_url,
        client_id=settings.MCP_AUTH0_CLIENT_ID,
        client_secret=settings.MCP_AUTH0_CLIENT_SECRET,
        audience=settings.MCP_AUTH0_AUDIENCE,
        issuer_url=settings.PUBLIC_BASE_URL,
        base_url=mcp_full_url,
        client_storage=storage,
        jwt_signing_key=settings.MCP_JWT_SIGNING_KEY,
        required_scopes=["openid", "profile", "email"],
        extra_authorize_params={"scope": "openid profile email offline_access"},
    )

    return auth


def get_user_from_token() -> User:
    token = get_access_token()
    if not token or not hasattr(token, "claims"):
        raise OAuthAuthenticationError("No access token available")
    return User.model_validate(token.claims)


def get_organization_id_from_token() -> str:
    return get_user_from_token().organization_id


def get_user_id_from_token() -> str:
    return get_user_from_token().user_id
