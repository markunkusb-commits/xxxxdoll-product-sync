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

配置由 `sync_worker.config.load_config()` 自动从项目根目录的 `.env` 读取，随后由同名进程环境变量覆盖。默认值为 `staging`、`DRY_RUN=true`、商品状态 `draft`，并强制 `ALLOW_DELETE=false`。模块使用标准库解析基础 dotenv 格式，不会输出配置值或执行网络请求。

`.env` 和 `.env.*` 已被 Git 忽略，仅 `.env.example` 可提交。请勿提交包含真实密钥的配置文件。

## 运行测试

无需安装第三方依赖：

```powershell
python -m unittest discover -s tests -v
```
