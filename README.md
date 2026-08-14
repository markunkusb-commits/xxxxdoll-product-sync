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

## 只读连接检查

项目使用 `src` 布局，先在本地以 editable 模式安装：

```powershell
python -m pip install -e .
```

然后运行：

```powershell
python -m sync_worker doctor
```

`doctor` 仅允许对已验证的 `wpcomstaging.com` HTTPS 主机执行 GET/HEAD，代码层会阻止所有写请求以及订单、客户、付款、优惠券和用户列表接口。运行前会再次确认 staging、dry-run、draft 和禁止删除等安全配置。输出报告写入 `reports/doctor-report.json`，只保留允许字段并统一脱敏。

## 参考产品结构检查

按精确 SKU 只读检查一个参考产品：

```powershell
python -m sync_worker inspect-product --sku MD-M001-150-A-SUSAN
```

命令只调用 WooCommerce 产品 GET 接口，并在安全检查通过后提取基础字段、分类、品牌、产品 taxonomy、属性、图片文件名、描述统计和 meta 白名单。未知 meta 只记录 key 并标记 `review_required`；HTML、完整图片 URL、认证信息以及未知 meta value 均不会写入报告。报告保存为 `reports/reference-product-<SKU>.json`，HTTP 写请求计数固定为零。

可以使用以下边界安全的正则扫描参考产品报告中的潜在密钥：

```powershell
Select-String `
  -Path reports\reference-product-*.json `
  -Pattern '(?<![A-Za-z0-9])(?:ck|cs)_[A-Za-z0-9]{20,}|Authorization|Cookie|WP_APP_PASSWORD'
```

## Google 只读权限检查

项目依赖官方 `google-api-python-client`、`google-auth` 和 `google-auth-httplib2`。配置必须使用项目目录外的服务账号 JSON，并严格设置以下只读 scopes：

```text
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/spreadsheets.readonly
```

安装项目依赖后运行：

```powershell
python -m sync_worker google-doctor
```

该命令只检查两个指定根文件夹、最多 100 个一级子项、Spreadsheet 元数据，以及每个工作表的 `A1:Z5` 小样本。报告只保存文件元数据和单元格统计，不保存文件 ID、Spreadsheet ID、单元格内容、凭据内容或下载链接；所有 Google 写操作均不在代码允许列表中。

## 供应商结构清单

```powershell
python -m sync_worker supplier-inventory
python -m sync_worker supplier-inventory --max-depth 4
```

该命令递归扫描 CLM 与 MD 文件夹（深度限制 1–6、每目录最多 500 项），并仅从各工作表读取 `A1:AZ10` 结构样本。报告写入 `reports/supplier-inventory.json`，不保存 Google 文件、文件夹、Spreadsheet 或 Sheet ID，也不会下载文件或图片。

## Sheet 版式检查

```powershell
python -m sync_worker inspect-sheet-layout `
  --sheet "RMB Price List" `
  --range "A1:AZ50"
```

该命令只读检查一个明确限制的 Sheet 区域（最多 100 行、52 列和 5200 个单元格），保留格式化显示值、真实 A1 坐标以及 merged ranges。报告不会保存公式原文，也不会执行任何写请求；输出文件为 `reports/sheet-layout-<safe-sheet-name>.json`，并由现有 `reports/*.json` Git ignore 规则保护。

## CLM RMB Price List Parser V1

`sync_worker.clm_price_parser` 是纯本地、无网络和无写入的中间模型解析器。它接收已经生成的 sheet-layout 结构，依据每个动态系列标题划分产品 Block，并提取规格、included features、upgrade options、价格、notice 与图片链接占位符。未知规格和商业字段会连同坐标保留，不会被静默丢弃；`Height(Model)` 保持为独立原始规格并附加 warning，不会猜测拆分。
