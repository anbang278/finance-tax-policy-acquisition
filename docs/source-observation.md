# 来源勘察与字段口径

勘察日期：2026-09-28。以下是当日网页与程序访问证据，不构成源站接口稳定性的保证。

| 字段或行为 | [财政部政策发布](https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/) | [税务总局政策法规库](https://fgk.chinatax.gov.cn/zcfgk/c100006/listflfg.html) |
|---|---|---|
| 栏目入口 | 静态 HTML 列表，分页使用 `index_N.htm`，页面脚本存在 `countPage` | 浏览器渲染列表；页面内部发起 `POST /getFileListByCodeId`，请求体含 `channelId/page/size` |
| 详情链接 | 可能指向财政部不同司局主机，部分 HTML 使用 `http` 字面链接 | 列表响应携带法规库详情 URL；正常浏览器访问页面 |
| 列表日期 | 栏目日期，记为 `listing_date` + `column_date` | 响应的 `writtendate`，记为 `listing_date` + `issued_date` |
| 成文/发布日期 | 不从栏目日期推断；正文未可靠标明时保持空值 | 列表成文日期可映射 `issued_date`；发布日期另行提取，无证据时为空 |
| 原始证据 | 保存列表 HTML、详情 HTML、附件及 SHA-256 | 已保存列表接口 JSON、详情 HTML、附件及 SHA-256；会话 Cookie 不落盘 |

财政部有界实测在列表第一页保存 1 份详情；标题《[关于印发〈紧急采购管理暂行办法〉的通知](https://gks.mof.gov.cn/guizhangzhidu/202609/t20260911_3997286.htm)》。该样本列表日期为 2026-09-14，正文包含办法全文；程序没有把栏目日期写入未证实的发布日期或成文日期。

税务站在本机的无界面 Playwright 中返回 HTTP 403；有界面 Playwright 正常导航返回 HTTP 200。页面请求体的 `channelId` 是栏目参数，未观察到单独的授权请求头。将该浏览器会话取得的 Cookie 与请求参数移交同批次 `requests.Session` 后，列表接口返回 HTTP 200。现有程序从浏览器成功请求中动态读取参数，不硬编码其值；未登录、不操作验证码，也不使用代理。

真实有界采集任务 `c739fea75bdf4c2eaa32186e410313d5` 保存税务列表第一页及 1 份公告详情。样本《[国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告](https://fgk.chinatax.gov.cn/zcfgk/c100012/c5252176/content.html)》的成文日期为 2026-09-04，正文提取为 262 字符；两个主附件分别为 Excel 和 Word 原件，均已保存但未解析，因此资料隔离。独立 Adapter 实测第一页和第二页各 10 条、首条 URL 不同；断点续跑重新建会话后又保存 1 份含可提取文本 PDF 的公告。全年覆盖、复杂分页中断和更多页面类型仍待验收。
