"""Action tools: placeholder for domain-specific actions.

Extend this module with your own actions for your specific use case.
Examples: create_quotation, assign_lead, generate_report, send_email, etc.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def placeholder_action(action_name: str, **params) -> str:
    """
    Placeholder action tool.

    Replace this with your domain-specific actions. For example:

        async def create_quotation(client_name: str, items: list, ...) -> str:
            # Your business logic here
            ...

        async def assign_lead(lead_id: str, agent_name: str) -> str:
            # Your business logic here
            ...

        async def generate_report(report_type: str, date_range: str) -> str:
            # Your business logic here
            ...
    """
    logger.info("Action requested: %s with params: %s", action_name, params)
    return (
        f"Action '{action_name}' is not yet implemented. "
        "To add custom actions, extend agent/tools/action_tools.py "
        "with your domain-specific functions."
    )
