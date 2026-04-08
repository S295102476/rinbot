import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter

nonebot.init(
    driver="~fastapi",
)

driver = nonebot.get_driver()
driver.register_adapter(OneBotAdapter)

# 加载所有插件
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run(host="127.0.0.1", port=8080)