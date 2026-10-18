"""Send mail through AgentMail's hosted MCP server.

The send step of the rejection flow calls the ``send_message`` MCP tool
deterministically (no LLM in the loop), so the human review in
``review_email_draft`` stays the only gate on what goes out. Auth is the same
AgentMail API key, passed as the ``x-api-key`` header.
"""

import logging

from app import agentmail_tools
from app.app_utils.tracing import traced_external
from app.config import AGENTMAIL_MCP_URL

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0


@traced_external("agentmail.mcp_send_email", system="agentmail")
async def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email from the AP inbox via the MCP ``send_message`` tool.

    Returns True on success, False on any transport or tool error.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        inbox = agentmail_tools._require_inbox()
        headers = {"x-api-key": agentmail_tools._api_key()}
        async with streamablehttp_client(
            AGENTMAIL_MCP_URL, headers=headers, timeout=_TIMEOUT_S
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "send_message",
                    {"inboxId": inbox, "to": [to], "subject": subject, "text": body},
                )
        if result.isError:
            logger.warning("mcp send_message returned an error: %s", result.content)
            return False
        logger.info("mcp send_email: sent to %s subject=%r", to, subject)
        return True
    except Exception as exc:
        logger.warning("mcp send_email failed: %s", exc)
        return False
