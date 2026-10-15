import os

USER_AGENT = "bookmark-agent/0.1 (personal weekly paper digest)"
if os.environ.get("BOOKMARK_AGENT_CONTACT"):
    USER_AGENT += f" contact: {os.environ['BOOKMARK_AGENT_CONTACT']}"
