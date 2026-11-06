# 企业所得税专题研究

从小微企业优惠、研发费用加计扣除、优惠办理与资料留存三个专题开始。使用既有运行入口和显式数据目录，逐步完成资料获取、语义复核、知识候选、独立预审、Wiki与研究报告。

## 获取专题依据

官方清单为JSON数组，每项给出来源、URL、标题、专题和选择理由。可选document_number用于核对预期文号。清单最多100项，来源必须与来源配置匹配。

```json
[
  {
    "source_id": "mof",
    "url": "https://www.mof.gov.cn/jrttts/202308/t20230802_3899936.htm",
    "title": "关于进一步支持小微企业和个体工商户发展有关税费政策的公告",
    "document_number": "财政部 税务总局公告2023年第12号",
    "topic_id": "cit-small",
    "reason": "小微企业资格与优惠期限的直接依据"
  }
]
```

```sh
ftr --data-dir /ABS/DATA collect --urls-file official-urls.json --date-from 2015-01-01 --date-to 2026-11-06 --max-documents 20
```

`--urls-file`自动选择targeted模式。也可在完整TaskRequest中填写`mode`和`official_urls`，通过`--request`提交。空清单、来源不一致及非法URL在业务写入前报错。清单冻结后走正文、附件、预算和恢复队列，范围报告标明`explicit_url_list`。网页实际标题和文号从原文提取，预期文号不匹配会显示资料限制。

查看任务报告中的下载、可读性、语义复核和缺失项。网络访问失败按实际原因处理，恢复后使用原任务续跑入口。政策归组在工作台“政策身份与资料覆盖”查看，展示候选身份、正文一致性、各来源版本与附件状态。

## 生成与查看Wiki候选

资料经过语义复核并建立证据索引后，按知识与Wiki流程创建extract任务。每条知识提供主引用、条件、例外和缺口。时间依据区分effective_from、effective_to、tax_period、filing_period与transition。法律关系提供目标资料、关系类型、涉及条款及定位引用。

`valid_to_kind`有specified、open_ended、unknown三种取值。明确日期需要valid_to，open_ended表达原文未规定终止。使用者核对期间后填写applicability_verified，日期检索才据此判断。

创建compile或revise任务时选择“候选预览”，可使用来源完整的未发布知识候选。预审使用独立precheck任务，outputs为空，checks按support、conditions、temporal、authority记录支持、冲突或不足，并绑定目标原文陈述和精确Anchor。

```json
{
  "job_type": "compile",
  "topic_id": "cit-small",
  "records": [],
  "knowledge_versions": ["实际知识版本ID"],
  "output_mode": "preview"
}
```

```sh
ftr knowledge job create --file compilation-request.json
ftr knowledge job export --id JOB_ID --output task-package.json
ftr knowledge job submit --file host-result.json
ftr wiki export --id OBJECT_ID --version CANDIDATE_VERSION --preview --output topic-preview.md
```

工作台可切换编纂方式、选取知识、阅读历史、下载预览和查看检查记录。日常发布依次完成知识审核启用、专题审核发布。预览中展示各依赖的核验状态。

## 形成研究报告

研究Skill先把业务问题拆成文号、主题和条件检索词，记录实际召回。context文件分别提供tax_period、filing_date、region、subject_facts、required_authorities和missing_inputs。报告as_of表示资料截止日。

```sh
ftr research prepare --query 研发 --context research-context.json
ftr research submit --file research-draft.json
```

ResearchDraft的每个claim包含text、kind、citations，并可填写conditions、exceptions和temporal_basis。首次提交返回定位校验、缺口及precheck_input_digest。独立预审读取同一研究背景和完整依据，为每条陈述补四个维度的checks，将摘要写入草稿的precheck_input_digest后再次提交。正文、背景或引用变化后重新预审。

使用完整、可独立理解的引文。导出报告附原文摘录、证据片段和左闭右开的字符区间，便于逐项核对。遇到转引其他政策时，保留被引文件名称和执行关系，继续核对相应正文。指定法源未召回会显示缺口。数值判断缺少所属期或时间依据、空稿、检查不全、引用冲突等情况显示待核实。

在本机管理工作台“研究报告校验与预览”选择研究JSON文件，查看逐项结果并下载Markdown。报告预览以只读方式检查数据，耗时原件校验在工作线程执行。

## 处理政策变化与迁移

有证据的修改、废止等关系候选会定位已发布知识及引用它的章节，进入影响队列。认领后生成修订任务，旧内容在处理期间显示待核。新版本审核发布后点击“确认修订已发布”，服务端核对发布身份、依赖资格和变化来源引用，再关闭对应记录。判断无影响时填写实际核查理由。

显式写入会将schema 0–5增量迁移到6，并在相邻目录创建迁移前备份。新表保存政策身份候选与来源记录映射。回退时在新目录恢复升级前备份，再使用相应旧程序。旧任务、研究草稿和无checks的旧回传仍可按兼容入口读取与重放。

试点数据、模型评测、源站试跑与工程验证分别记录于[专题升级记录](../verification/cit-research-loop/REPORT.md)。
