# xxxxdoll-product-sync

Python 3.12 项目，用于搭建未来商品同步 worker 的安全基础。本阶段只包含项目结构、环境变量配置读取和安全检查，不包含任何 WordPress、WooCommerce 或 Google Drive 客户端，也不会执行网络请求。

## 项目结构

```text
.
├── configs/              # 非敏感配置预留目录
├── reports/              # 本地运行报告目录
├── src/sync_worker/      # 应用代码
├── tests/                # 标准库 unittest 测试
├── .env.example          # 空白配置模板
└── pyproject.toml        # Python 3.12 项目元数据
```

## 配置

配置由 `sync_worker.config.load_config()` 从进程环境变量读取。默认值为 `staging`、`DRY_RUN=true`、商品状态 `draft`，并强制 `ALLOW_DELETE=false`。

如需本地配置，可复制 `.env.example` 的字段到你自己的环境变量管理方式中。请勿创建或提交包含真实密钥的配置文件。本项目不会自动读取 `.env` 文件。

## 运行测试

无需安装第三方依赖：

```powershell
python -m unittest discover -s tests -v
```
