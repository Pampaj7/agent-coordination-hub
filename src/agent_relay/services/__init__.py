"""Business logic. The API layer is a thin shell over these functions.

Keeping the logic here (rather than in the routers) is what makes a future
Coordinator Agent cheap: it can import these directly, or call the HTTP API, and get
identical answers.
"""

from agent_relay.services.github import GitHubService
from agent_relay.services.slack import SlackNotifier, format_event

__all__ = ["GitHubService", "SlackNotifier", "format_event"]
