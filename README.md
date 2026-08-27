# xxxxdoll-product-sync

Python 3.12 项目，包含商品数据的本地解析、映射与 dry-run，以及显式运行的只读 WordPress/WooCommerce、Google 检查命令。只读检查会访问网络；单元测试使用 mock，不读取真实供应商数据或凭据，不执行外部写请求。

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

项目依赖官方 `google-api-python-client`、`google-auth`、`google-auth-httplib2` 和 `PySocks`。配置必须使用项目目录外的服务账号 JSON。仅 legacy full workflow（`google-doctor` / `supplier-inventory`）要求同时提供两个 Drive folder ID，并严格设置以下 scopes：

```text
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/spreadsheets.readonly
```

安装项目依赖后运行：

```powershell
python -m sync_worker google-doctor
```

该命令只检查两个指定根文件夹、最多 100 个一级子项、Spreadsheet 元数据，以及每个工作表的 `A1:Z5` 小样本。报告只保存文件元数据和单元格统计，不保存文件 ID、Spreadsheet ID、单元格内容、凭据内容或下载链接；所有 Google 写操作均不在代码允许列表中。

### 按 workflow 隔离 scope

以下 scope 后缀均以 `https://www.googleapis.com/auth/` 为前缀，配置必须精确匹配：

| Workflow | Drive scope | Sheets scope | 客户端 |
| --- | --- | --- | --- |
| `google-doctor` / `supplier-inventory` | `drive.readonly` | `spreadsheets.readonly` | Drive + Sheets |
| `inspect-sheet-layout` / `discover-mapped-media-sources` | 不校验、不申请 | `spreadsheets.readonly` | 仅 Sheets v4 |
| Drive metadata Core | `drive.metadata.readonly` | 不校验、不申请 | 仅 Drive v3 |
| `build-drive-folder-manifests` / `build-nested-drive-folder-manifests` / `build-depth2-drive-folder-manifests` | `drive.metadata.readonly` | `spreadsheets.readonly` | Drive metadata + Sheets |

所有路径都先检查 proxy 和项目目录外的服务账号文件。Sheets-only 还检查 Spreadsheet ID，但不要求 Drive folder IDs；纯 Drive metadata Core 不要求 Spreadsheet ID。Manifest 命令因为需要 fresh Sheets reference，会同时检查 Sheets scope 和 Spreadsheet ID。

`GOOGLE_DRIVE_SCOPE` 是共享配置项，单个值不能同时满足 legacy full 与 metadata-only workflow。`.env.example` 保留 legacy 示例值；运行 Manifest 时必须显式配置为 `drive.metadata.readonly`，不能为了兼容 legacy 扩大 Manifest 权限。已使用 metadata scope 的配置无需为两个 Sheets-only 命令切换 scope。需要运行 legacy 命令时，请为该次运行明确选择对应配置；不要拼接两个 Drive scopes，也不要新增可写 scope。

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

## Media / Drive 正式主链

以下命令会执行受限的真实只读请求，仅在人工确认配置与当前 snapshot 后运行；它们不是离线 parser：

```powershell
python -m sync_worker discover-mapped-media-sources `
  --mapping reports/image-mapping-dry-run.json `
  --sheet "RMB Price List" `
  --sku-report reports/sku-dry-run.json

python -m sync_worker build-drive-folder-manifests `
  --mapping reports/image-mapping-dry-run.json `
  --sheet "RMB Price List" `
  --sku-report reports/sku-dry-run.json
```

使用同一份当前 product snapshot 生成的 mapping 和 SKU 报告，按精确 source row range 关联，不使用 stale 报告中的脱敏 URL 作为来源。Secure Media Reader 将 mapping 批准的 exact cells 合并为一次 Sheets GET；依次保留 hyperlink、rich-text link、cell-level link、Smart Chip、静态 `HYPERLINK` 公式及 direct URL fallback 的兼容读取，多链接歧义仍阻断。URI 仅保留在内存，不执行额外探测请求。

Manifest 链路为 Secure Reference Read → SKU exact-range join → fresh in-memory Media Discovery → `SecureGoogleDriveFolderHandle` → metadata-only `files.list`。只列一级子项（有界分页），不递归 nested folders、不跟随 shortcuts，不使用 `get_media` / `alt=media` / export，不下载。报告 `reports/google-drive-folder-manifest-dry-run.json` 只保留业务字段、ID fingerprints、安全状态及 Sheets/Drive/network/download/write 计数，不保留 raw URL、原始 ID、resource key 或临时 diagnostic 探针；下载和外部写计数始终为 0。

临时命令 `diagnose-media-cell-shapes` 和 `diagnose-media-reference-parity` 已移除。

### Nested Folder Manifest Dry Run

以下命令同样会执行真实只读 metadata 请求，需在人工确认后运行：

```powershell
python -m sync_worker build-nested-drive-folder-manifests `
  --mapping reports/image-mapping-dry-run.json `
  --sheet "RMB Price List" `
  --sku-report reports/sku-dry-run.json
```

每次运行在同一进程重新建立 exact-cell Sheets read → SKU exact-range join → fresh Media Discovery → Root handle → Root `files.list` → Nested handle → Nested `files.list`。只读取 mapping 和 SKU 两份本地输入，不读取旧 Root Manifest 报告，不从 fingerprint 反推 ID。Root domain item 的 `provider_file_id` 仅在内存中传给 Nested Core；报告不保留 raw IDs、URL 或 resource key。

只遍历 depth 0 → 1，最多 100 个 Nested folders，复用 Core 的有界分页和重试。所有一级目录均读取，不按 Photos / Factory Photos / Videos / Banner 名称过滤。Root 和 Nested shortcuts 均不跟随；depth 2 文件夹仅记录 `nested_folder` 与 `max_traversal_depth_reached`，不再请求。共享 Nested folder 保留各产品独立 manifest，并标记 `shared_nested_folder_candidate`；不选择主图或生成图库。

输出 `reports/google-drive-nested-folder-manifest-dry-run.json`，包含 `summary`、安全的 Nested `results`、失败/阻断 Root 的 `root_issues`、warnings 和 blocking issues。Summary 分别统计 Root/Nested 页数及 Sheets、Root Drive、Nested Drive 调用次数（含重试），总读取为三者之和；下载和外部写请求始终为 0。完整单页 mock 场景的 8 Root + 24 Nested 为 33 次读取，这不是固定上限。Root 读取不完整时不继续遍历该 Root；有阻断的报告标记 `partial`，CLI 返回 1；配置或批次上限等错误返回 2，不写出新的成功报告。

### Depth-2 Folder Manifest Dry Run

以下命令会执行受限的真实只读 metadata 请求，不是离线 parser；本阶段开发只用 mock 验证，真实运行须由人工确认：

```powershell
python -m sync_worker build-depth2-drive-folder-manifests `
  --mapping reports/image-mapping-dry-run.json `
  --sheet "RMB Price List" `
  --sku-report reports/sku-dry-run.json
```

同一进程重新建立 Sheets exact-cell read → SKU exact source-range join / Media Discovery → Root → depth-1 → depth-2 domain-object 链。只加载 mapping 和 SKU 两份当前本地输入，不读取旧 Root/Nested Manifest 报告，不从 fingerprint、SKU 或文件名推断 Drive ID。各层原始 ID 仅在内存传递；输出前执行白名单投影、脱敏和已知 ID 泄漏检查。

Depth-2 仅消费成功 depth-1 manifest 中 `item_kind=nested_folder` 且带 `max_traversal_depth_reached` 的实际 item，由现有 Core 验证内存 `provider_file_id`。缺失 ID 会报告阻断，不猜测；目标数量随新读取结果变化，不固定为 8。超过 50 个目标，在任何 depth-2 请求前以 `depth2_folder_batch_limit_exceeded` 停止。现有 Root/Nested 命令的深度边界不变。

最大 traversal depth 为 2，depth-2 返回的更深 folder 仅记录并标记 `max_traversal_depth_reached`，绝不再次读取；所有层级 shortcuts 均不跟随。不按 Photos、Factory、Videos、Banner、Eye Options、Promo 或 Skin Tone 名称过滤，不生成 folder role 或选择主图/图库。图片候选保持 MIME 判断，包括 PSD 的现有行为；不下载、不导出、不探测媒体内容。

输出为 `reports/google-drive-depth2-folder-manifest-dry-run.json`，含安全的 `results`、`summary`、上游 `root_issues` / `depth1_issues`、warnings 和 blocking issues。逐层统计 Root/Nested/Depth-2 页数和 Sheets/Root/Nested/Depth-2 API 读取次数（含分页、重试），总读取为四者之和：单页 mock 的 1 + 8 + 24 + 8 = 41 不是硬编码值。下载和外部写请求固定为 0。存在读取失败或阻断时报告 `partial`、CLI 返回 1；配置、批次上限或安全扫描失败返回 2，不覆盖已有报告。只有成功生成的新报告才能作为本次 Reality Check 依据。

## CLM RMB Price List Parser V1

`sync_worker.clm_price_parser` 是纯本地、无网络和无写入的中间模型解析器。它接收已经生成的 sheet-layout 结构，依据每个动态系列标题划分产品 Block，并提取规格、included features、upgrade options、价格、notice 与图片链接占位符。未知规格和商业字段会连同坐标保留，不会被静默丢弃；`Height(Model)` 保持为独立原始规格并附加 warning，不会猜测拆分。

使用本地 sheet-layout JSON 生成脱敏 dry-run 报告：

```powershell
python -m sync_worker parse-clm-price-list `
  --input reports/sheet-layout-RMB-Price-List-A45-AZ100.json
```

该命令不会加载 `.env`、服务账号或 Google API 客户端，也不会发起网络或外部写请求。输出固定写入 `reports/clm-parser-dry-run.json`；报告只保留允许的产品结构，供应商 URL 会在写入前脱敏。

## WooCommerce Payload + Staging Category Binding Dry Run

默认不选择任何 Woo category binding profile，因此不会向 payload 添加分类，并输出 `category_binding_not_selected`。如需使用已人工批准的测试站 Binding，必须同时显式提供 profile、本地 discovery 报告和目标站 URL：

```powershell
python -m sync_worker build-woocommerce-payloads `
  --products reports/clm-parser-dry-run.json `
  --sizes reports/size-list-dry-run.json `
  --presented-options reports/product-option-presentation-dry-run.json `
  --category-binding-profile xxxxdoll-staging-category-bind-v1 `
  --woo-category-discovery reports/woo-category-discovery.json `
  --target-base-url https://staging-1d07-owenau512-iqjhz.wpcomstaging.com
```

命令只读取本地 JSON。只有 internal category mapping、binding target 名称验证和 staging host 验证全部成功时，payload 才会包含 `categories: [{"id": ...}]`；Classic/ULW、正式站 host、缺失或改名的 discovery target 均不会 fallback。输出仍固定为 draft/simple、`ready_for_write=false`、网络请求计数 0 和外部/API 写请求计数 0。
