# Feed 与 Evolve 数据集选择

Feed 节点的 Inspector 现在有 Dataset version 和 Split。版本与 split 保存在节点 source.dataset / source.split 中；旧节点没有 dataset 时仍使用 contemporary，保证已有画布不被默默切换。新建或编辑时可以自行选择发布版本。

Batch run 选择 credit_risk feed 时也可单独选择版本与 split；选择 Canvas source 时使用节点保存的配置。对于发布版，n 表示轨迹数（0 为整个集合），Walk each window 可选 none/daily/weekly/monthly。none 每条轨迹一次；daily 保留原始非空观察日；weekly 从窗口开始每 7 天一段，monthly 按自然月边界分段。周/月只合并该周期内新增材料，最后不足一周期的材料也保留，空周期跳过。每次 as_of 是周期结束日（不超过窗口末日），不会读取未来材料。预览与实际运行使用同一步长。Single run 的计划会要求明确选择一条观察或改用 Batch run。

Evolve 的 Data to learn from 支持 Dataset version、Split、Samples；版本信息记录在优化任务 source 中并在结果页显示。Upload JSONL 的原有入口继续可用。当前发布目录中有 cases、observations、outcomes、partitions，且只有 dev/test 分区的版本会被发现。

当前随机 6:4 版 `2026-09-10-random-dev-test-v1`：

| split | 轨迹 | 观察 | 现有 Evolve 评分器可用的窗口 |
|---|---:|---:|---:|
| dev | 64 | 1445 | 30 |
| test | 43 | 707 | 23 |

上表观察数是 daily 模式；Feed 可按所选步长聚合全部所选轨迹的观察输入。Evolve 仍使用现有窗口快照评估器，而非逐日运行并累积 Memory 的 trajectory 评估器。为适配现有 credit_risk 分数，只将已核验、注册主体在窗口结束后发生的 Chapter 11 / Chapter 7 / Canadian bankruptcy assignment 事件映射为 positive。未核验负样本、子公司事件、状态更新窗口等不自动编造负标签。界面明确列出这个限制和 eligible cases 数量；因此此模式不能用于宣称完整数据集准确率或误报率。

发布版 sample_json 是只含输入材料的兼容格式，不包含 outcomes、label 或 event_date。Evolve 通过 case_id 在服务端单独关联 label。普通新版 Feed 批次不通过 sample_json 自动推导评分标签；没有显式 label_key 时，会拒绝开启旧式自动评分，用户仍可无评分运行全部观察。

选择 test 启动 Evolve 会把该集合用于优化器内部的训练/验证划分，不等于只做独立测试。当前工作只接入数据选择，不更改优化器算法或内部切分方法。


## Evaluation & Evolve modes (2026-09-10)

The unified panel offers `Evaluation only` and `Evolve + Evaluation`, sharing
source selection, metrics, persistent history and record inspection.
`mode=evaluate` on the existing `/api/graphs/{graph_id}/evolve` endpoint uses
all selected eligible records, including a single-record selection, with the
original prompts. It does not construct an optimizer or produce an applicable
optimized graph. `mode=evolve_evaluate` (also the default for older clients)
retains baseline evaluation, prompt optimization and optimized evaluation.

Both modes currently use the existing framework **window snapshot evaluator**,
not the canvas runner's trajectory replay or memory. Combined mode scores the
candidate-validation subset before and after optimization; this is not an
independent held-out test score. Dataset eligibility rules remain unchanged.
The source dataset and its dev/test partition are not modified.


### Saved results are the default

The panel now defaults to Evaluation only with a saved batch. A completed,
failed or cancelled batch, or an individual stopped/finished run, can be selected.
`source=saved_batch, batch_id=...` or `source=saved_run, run_id=...` reads stored
outputs and traces; it never invokes the workflow runner. Optional `label_key`
selects an expected-answer input field. Missing labels remain unscored.
For release credit-risk results, reviewed outcome sidecars are joined only for
the trajectory report; eventual events do not become daily risk labels.

With these sources, Evolve proposes prompts from saved traces in one model
request (up to 40 traces, failures first, with bounded excerpts). It does not
run MIPRO candidate search, repeat the baseline or validate new prompts.
The original graph is unchanged until Apply/Save is explicitly selected.
History displays proposals as unvalidated and never invents an after score.
Dataset/upload replay paths remain explicitly labelled as workflow reruns.


### Selecting a partition of saved results

Saved-result Evaluation and Evolve both accept `dataset` and `split` (`dev`,
`test`, or empty for all). The dataset defaults to the run's recorded version.
Records are matched by case/sample ID against the selected version's partition
file. Preview reports matching records, trajectories and dataset cases without
saved results. Missing cases are never executed or fabricated, and an empty
intersection is rejected. The selected version and partition are saved in history.
Changing a version reinterprets partition membership; it does not regenerate inputs.
