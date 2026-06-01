from skills.app_management import ALL_TOOLS as APP_TOOLS
from skills.storage import ALL_TOOLS as STORAGE_TOOLS
from skills.healthcheck import ALL_TOOLS as HEALTHCHECK_TOOLS
from skills.message_bus import ALL_TOOLS as MESSAGEBUS_TOOLS
from skills.filesystem import ALL_TOOLS as FS_TOOLS
from skills.shell import ALL_TOOLS as SHELL_TOOLS
from skills.photos import ALL_TOOLS as PHOTOS_TOOLS
from skills.wiki import (
    wiki_get_node, wiki_list_full_tree, wiki_recent_changes,
    wiki_append_user_notes, wiki_replace_user_notes, wiki_register_root,
)
from skills.skills_registry import ALL_TOOLS as SKILLS_REGISTRY_TOOLS
from skills.search import SEARCH_TOOLS

# Wiki tools — write tools added in Task 7.
WIKI_TOOLS = [
    wiki_get_node, wiki_list_full_tree, wiki_recent_changes,
    wiki_append_user_notes, wiki_replace_user_notes, wiki_register_root,
]

ALL_TOOLS = (APP_TOOLS + STORAGE_TOOLS + HEALTHCHECK_TOOLS
             + MESSAGEBUS_TOOLS + FS_TOOLS + SHELL_TOOLS + PHOTOS_TOOLS
             + WIKI_TOOLS + SKILLS_REGISTRY_TOOLS + SEARCH_TOOLS)
