from skills.app_management import ALL_TOOLS as APP_TOOLS
from skills.storage import ALL_TOOLS as STORAGE_TOOLS
from skills.healthcheck import ALL_TOOLS as HEALTHCHECK_TOOLS
from skills.message_bus import ALL_TOOLS as MESSAGEBUS_TOOLS
from skills.filesystem import ALL_TOOLS as FS_TOOLS

ALL_TOOLS = (APP_TOOLS + STORAGE_TOOLS + HEALTHCHECK_TOOLS
             + MESSAGEBUS_TOOLS + FS_TOOLS)
