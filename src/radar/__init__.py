"""AI Tech Radar 的核心 Python 包。

包内包含候选项目发现、GitHub 数据采集、快照存储、趋势评分和只读查询
API。采集与评分的基础安装只依赖 Python 标准库，PostgreSQL 和 HTTP API
分别通过可选依赖启用。
"""

# 版本号同时用于打包元数据和对外展示，升级功能时应保持二者同步。
__version__ = "0.1.0"
