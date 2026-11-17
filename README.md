# 光伏电站限电事件与损失电量复算

给新能源场站运营分析员使用。仅用有效对照逆变器和相同辐照区间拟合基线；重叠限电事件先求并集再积分。

七个业务模块：遥测采信、设备映射、事件切片、基线拟合、损失核算、能量对账、运行归档。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18244 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验。完整对照批INV-A重叠事件取并集后30分钟，基线100kW、实际60kW，损失20kWh；无合格对照或遥测冲突必须保持未定，不能填0。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy）、`csv_text` 与 `overrides` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。

## 设备归属更正后的修订归档

设备台账（区域、额定功率、对照资格）更正后，旧批损失需要重算。修订采用**整批重算 + 片段级回植**：先按更正后的台账完整复算（为了赶当天口径，最快），再逐片段与原批比对；计算值未变的片段沿用原批原值与原精度，保证复核可比；只有计算值变化的片段采用新结果并挂上原批旧值、更正依据和变化字段。

```sh
# 发起修订：必须带更正依据，否则拒绝
curl -sX POST localhost:18244/api/runs/<原批>/revisions -H 'Content-Type: application/json' -d '{"corrections":[{"device":"REF-A1","field":"capacity_kw","to":"200","basis":"台账更正单 PCS-2026-1015","reason":"铭牌容量录入有误","actor":"运维张工"}]}'
curl -s localhost:18244/api/runs/<修订批>                  # 含 diff / blockers / closed
curl -sX POST localhost:18244/api/runs/<修订批>/publish     # 仅闭合草案可切换
curl -sX POST localhost:18244/api/runs/<修订批>/rollback    # 撤销切换，恢复旧正式版
```

- 修订批按“原批 + 组装后结果”内容寻址，同一更正可复现已有的修订版。
- `closed=false` 时存在阻断项（结构变化、无法解释的片段变化、已确定变未定、未定增加、总差额不闭合、守恒失败、隔离账变化、更正无效果、原批未达可归档）。未闭合草案可保存、可导出，**不能替换正式版本**。
- 正式切换是单事务：失败整体回滚，旧版保持正式并继续可导出核验。
- 旧正式批不可变，`formal=0` 后仍保留原摘要与证据，`GET /api/export/{原批}` 内容不变。
- 归档包对修订批额外含 `revision.json`（更正依据、新旧差异、阻断项、操作留痕）；原批归档不含该文件，按原样核验。
- 页面 `/archive` 展示更正依据、逐片段新旧基线/损失与差额、阻断项、谱系，并提供发布/回滚按钮（未闭合时发布按钮禁用）。
