# 公开来源目录与有界采集

本轮把来源范围从两个固定枚举扩展为受信任登记的栏目ID。`mof`、`chinatax`仍指向原栏目；新增栏目必须单独选择，旧计划不会自动扩大。实际覆盖和未接入项以[来源扩展验证](../verification/source-expansion/README.md)为准。

## 使用入口

工作台左下“查看采集来源”可按地区、机构、类别筛选并多选，设置日期和每来源页数/正文预算后预览冻结范围。下载的任务请求可交给CLI执行；“用于立即获取选源”只把选择带到管理页，仍需预览本轮窗口并显式确认获取。日常计划的范围单独保存，目录选择不改变定时授权。工作台默认只读。

```sh
uv run ftr sources --region 上海 --category tax
uv run ftr collect --sources tax_shanghai_policy --date-from 2026-11-01 --date-to 2026-11-06 --max-pages 2 --max-documents 3 --preview
uv run ftr --data-dir ./sample-data collect --request ./policy-source-request.json
```

`collect --preview`不建立数据目录。预览结果的`data.request`才是任务请求；网页下载文件直接包含该请求。新请求的外部快照必须与当前登记一致，否则重新预览。已保存任务续跑使用原来的栏目、日期语义及受控公共profile快照（原mof/chinatax活动Repair规则仍按既有门禁与版本审计），并继续受当前允许主机约束。

## 登记与内容语义

`config/sources.yaml`是工程受控信任表，ID绑定具体栏目。登记包含机构、站点地区、六类标签、真实入口、精确允许主机、日期语义、HTTP/浏览器方式和受限解析profile。未定位入口不得用首页替代；未完成规则或实采的项必须保留缺口，不记为已通过。

`DocumentRecord.source_publisher`表示发布站点机构；`issuing_authorities`与`applicable_regions`按正文有证据的字段提取，无法确认时为空，不从网站地区推断适用地区。`content_stage`区分正式、草案/征求意见和待核实；官方解读与官方业务问答分别标注。

同文转载继续保留各来源记录及版本，并进入现有政策身份、语义复核、知识提取、Wiki和研究流程。保存成功不自动启用知识或发布Wiki。

`collection_notes`保存DOM与非内容资源说明，显示在证据页；这些说明不替代内容复核，也不单独禁止人工审核。`limitations`继续记录正文、附件等硬缺口。浏览器DOM必须同时保存原始响应才能进入复核。既有记录不因说明分类变化而改写质量，重新提取产生新候选。

## 网络与附件

日常代理参数仍在RuntimeSettings。当前本机诊断确认既有Shadowrocket HTTP监听`http://127.0.0.1:1082`可访问财政部栏目；隔离样本显式使用该代理，未修改系统网络或用户日常配置。该地址只适用于存在相同本机代理监听的环境。

静态列表复用HTTP客户端；动态列表使用正常独占浏览器会话。浏览器DOM与原始导航/公开XHR响应分开存证，第三方或跨主机请求仍须精确登记，Service Worker被禁用以保持请求门禁。验证、限访和重定向失败会明确记录，不绕过站点访问限制。

HTML保留表格结构，文本PDF继续提取。Office、WPS、ZIP、OFD等保存原件并显示未支持解析；扫描PDF标为扫描件。补正文、补附件均沿用原来源和冻结任务范围。

## 调度与迁移

新计划使用`{schema_version:"2",settings:{...},scope:{source_ids:[...]}}`。业务来源范围不进入SchedulerSettings。旧扁平计划兼容原两个来源；旧计划编辑保留既有范围。窗口和管理队列冻结计划版本及来源快照。移出的来源停止后续调度，历史和检查点保留；重新加入后沿用原状态。持久游标支持三个及以上来源公平轮转。

SQLite v7只在显式写入路径迁移，迁移前生成相邻备份。Web只读查询不触发迁移。v7为managed_plan增加scope_json，旧值默认两个原来源；任务快照保存在request_json。恢复使用现有backup verify/restore命令，恢复目标应为新目录。旧任务正文和复核结果不覆写。

setup接受单个或逗号分隔的登记ID，如`scripts/setup.sh mof,tax_shanghai_policy`，根据所选来源是否需要浏览器准备环境。`all`保留原兼容含义（原两个来源及工作台），不会授权或启动所有新增来源。

## 复现样本

```sh
uv run python scripts/source_expansion_samples.py --output .ftr-data/my-source-samples --proxy http://127.0.0.1:1082 --sources tax_shanghai_policy
```

该命令会真实联网，只在已有采集授权时执行。每栏目最多读取两页列表，冻结实际观察到的样本日期，再定向读取前三个不同正文及关联附件，写入逐来源隔离目录。正常空列表与异常壳页分别报告。重复任务检查不扩充选定正文；`--retry`保留前次结果和数据库，并在新尝试子目录重新读取列表与正文；同一尝试内另行检查同键重复执行，后者不宣称已重新读取正文。脚本不会更改复核状态、启用定时服务或操作现有工作台。
