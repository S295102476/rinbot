"""RinBot entry point; start from this directory or use start.sh."""
import os
from pathlib import Path
from dotenv import load_dotenv
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from runtime_config import enabled_plugins, feature_enabled, load_config, validate_config

os.chdir(Path(__file__).resolve().parent)
load_dotenv(override=False)
try:
    config = load_config()
    validate_config(config)
except ValueError as exc:
    raise SystemExit(str(exc)) from None

server = config.get("server") or {}
nonebot.init(driver="~fastapi", host=os.getenv("HOST", server.get("host", "127.0.0.1")),
             port=int(os.getenv("PORT", server.get("port", 8080))))
driver = nonebot.get_driver()
driver.register_adapter(OneBotAdapter)


@driver.on_startup
async def initialize_schema():
    # Register before plugin hooks. All model modules have loaded by startup,
    # so migrations/queries cannot run against missing tables on a clean DB.
    from plugins.db import Base, engine
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


# Explicit handler list: utility modules and disabled external integrations are
# never discovered/imported just because a file exists in plugins/.
plugin_names = {"plugins." + name for name in enabled_plugins(config)}
if feature_enabled(config, "link_parser"):
    plugin_names.add("nonebot_plugin_parser")
loaded = nonebot.load_all_plugins(plugin_names, [])
missing = plugin_names - {plugin.module_name for plugin in loaded}
if missing:
    raise SystemExit("Plugins failed to load: " + ", ".join(sorted(missing)))

if __name__ == "__main__":
    nonebot.run()
