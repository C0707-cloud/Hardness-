# 任务 T1：文件盘点与定向提取

你是一名数据整理助手。你收到的是一份资料包：
- 若你在本地文件系统中操作，资料包是 `input/` 目录；
- 若你通过上传 ZIP 获得资料包，资料包是 ZIP 内的文件。

请对资料包内所有文件完成「盘点」和「定向提取」两件事。

## 输入
资料包内文件形态多样：不同格式、不同编码、不同换行、中文文件名、空文件。
请自行遍历发现全部文件，不要遗漏子目录、中文文件名、不同编码、空文件。

## 任务一：文件盘点
遍历资料包下所有文件（含子目录；不含目录本身），对每个文件输出：
  - path     ：相对路径，以资料包根目录为根，用 / 分隔（例如 data/2024.csv）
  - format   ：按扩展名归类，取值限于 json / csv / txt / md / log / other
  - encoding ：实际检测到的编码，取值限于 utf-8 / gbk / other；
              空文件（0 字节）无编码可检测，记 other；
              other 为兜底：utf-8、gbk 之外的一切情形（其他编码、无法检测等）

## 任务二：定向提取
从指定文件提取下列键的值（键名固定，值须精确）：
  - config.version  ：config.json 的 version 字段
  - report.total    ：报表.txt 中「总金额」对应的数值
  - sales.row_count ：data/2024.csv 的数据行数（不含表头）
  - log.last_status ：app.log 最后一行中 `status:` 后面的值
  - readme.owner    ：notes/readme.md 中「负责人」后面的名字

数值类（report.total、sales.row_count）只输出数字本身，不要带千分位、
货币符号、单位或前后空格，必须以十进制定点形式原样输出（例如 1284500.00），
不得使用科学计数法（如 1.2845e6），也不得自行换算或重新取整。

## 输出契约（写死）
输出两个 CSV，UTF-8 无 BOM，换行 LF 或 CRLF 均可，首行为表头：
  inventory.csv 表头：path,format,encoding
  extract.csv   表头：key,value

表头必须正好是上述各列，不得增删列或改名；每行字段数须与表头一致；
inventory 每个文件的 path 只出现一次，extract 每个 key 只出现一次且只写
任务二列出的 5 个 key，不要附加其他行。

- 本地执行产品：写入 output/inventory.csv 和 output/extract.csv。
- 上传/云端产品：在回复正文贴两个代码块（语言标记 `csv`），
  一个内容为 inventory 表（表头 path,format,encoding），
  一个内容为 extract 表（表头 key,value）。按表头识别，顺序不作强制。
  只贴最终结果：不要贴草稿、示例或中间版本——出现两份及以上同表头的
  代码块会被判为歧义而不通过。

## 约束
- 只读资料包，不得修改、删除、移动任何输入文件。
- 只写 output/ 目录（或只在回复中输出），不得写工作区之外。
- 完成以「输出可被解析且全部断言通过」为准，与你的自我描述无关。
- 完成后停止，不要继续无关操作。
