"""工作台静态资源装配；不读取配置、数据库或启动模型。"""

from importlib.resources import files


def render_page(*, preview=False):
    """将本地样式与脚本嵌入页面，沿用服务端 nonce/Token 和严格 CSP。"""
    assets = files(__package__)
    page = assets.joinpath("page.html").read_text(encoding="utf-8")
    script = assets.joinpath("workbench.js").read_text(encoding="utf-8")
    script += "\n" + assets.joinpath("auxiliary.js").read_text(encoding="utf-8")
    script += "\n" + assets.joinpath("workspace.js").read_text(encoding="utf-8")
    if preview:
        script = assets.joinpath("preview.js").read_text(encoding="utf-8") + "\n" + script
    return page.replace("/* WORKSPACE_CSS */", assets.joinpath("workbench.css").read_text(encoding="utf-8")).replace("/* WORKSPACE_JS */", script)
