# 88 个窗口的结局核验（2026-09-09）

当前发布：`../expansion/releases/2026-09-09-outcomes-v2/`。保留 100 家、107 个窗口、4,427 份独立材料、2,152 次观察。

## 核验结果

- 本轮 64 个原正样本窗口已核对事件类型、法律事件日期和主体；40 个日期与原候选/旧标签不同。
- 24 个原负样本完成有限检索审阅，但未达到无事件标签的证据要求，状态为 `negative_followup_insufficient`，event_type 仍为 `unverified`。
- 加上已有 19 个核验窗口，当前共 83 个有事件核验、24 个结局未认证。所有逐时点风险等级仍未标注，gold_samples=0。
- 未将结局文件、事后披露或核验结论写入模型观察输入。first_public_at 尚未逐条核对，保持 null。

## 关键修正

- ABC、州法接管、加拿大 CCAA、加拿大破产转让、爱尔兰清盘申请与美国 Chapter 11 分开。
- Olenox 是 SG Echo 子公司申请；Twin Hospitality 与 FAT Brands 属于同一集团程序，不能当成独立风险事件。
- Sonder 的停业/清算公告早于实际申请，实际 Chapter 7 申请日是 2025-11-14。
- NaturalShrimp 初始接管发生于 2024-09-09；Former BL Stores 即原 Big Lots，同日申请 Chapter 11。二者校正后原缓存为空，已补入 2024-08-14 和 2024-06-06 的一手披露摘要；仍是单条材料的稀疏窗口。
- Diamondhead 的 2025-07-31 是强制 Chapter 7 救济令，最初强制申请在 2024-06-12；窗口可能包含早期程序，不应宣称首次风险提前预测。

## 逐家公司结论

| 公司 | 原日期 | 核验日期 | 事件类型 | 来源 |
|---|---|---|---|---|
| Gritstone bio, Inc. | 2024-10-10 | 2024-10-10 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1656634/000119312524235637/d119736d8k.htm) |
| Mondee Holdings, Inc. | 2025-01-15 | 2025-01-14 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1828852/000182885225000013/mondee-assetpurchaseagreem.htm) |
| Independence Contract Drilling, Inc. | 2025-01-15 | 2024-12-02 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1537028/000153702825000003/icdi-20250109x8k.htm) |
| Bright Green Corp | 2025-01-28 | 2025-02-22 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1886799/000149315225008512/form8-k.htm) |
| CareMax, Inc. | 2025-02-03 | 2024-11-17 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1813914/000095017024130421/cmax-ex2_1.htm) |
| BurgerFi International, Inc. | 2024-09-11 | 2024-09-11 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1723580/000110465925010948/tm255917d1_ex99-4.htm) |
| Avinger Inc | 2025-02-10 | 2025-02-10 | assignment_for_benefit_of_creditors | [一手披露](https://www.sec.gov/Archives/edgar/data/1506928/000143774925003321/avgr20250208_8k.htm) |
| Omega Therapeutics, Inc. | 2025-02-14 | 2025-02-10 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1850838/000119312525027349/d896578d8k.htm) |
| NaturalShrimp Inc | 2025-02-18 | 2024-09-09 | receivership_order | [一手披露](https://www.sec.gov/Archives/edgar/data/1465470/000149315226034187/form10-k.htm) |
| Spirit Airlines, Inc. | 2024-11-18 | 2024-11-18 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1498710/000095010324018522/dp222864_ex9901.htm) |
| IntelGenx Technologies Corp. | 2025-02-28 | 2025-02-28 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/1098880/000106299325003890/form8k.htm) |
| CUTERA INC | 2025-03-05 | 2025-03-05 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1162461/000119312525046217/d878024d8k.htm) |
| ENGLOBAL CORP | 2025-03-06 | 2025-03-05 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/933738/000165495425002332/eng_8k.htm) |
| DZS INC. | 2025-03-14 | 2025-03-14 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/1101680/000110168025000021/dzsi-20250314.htm) |
| Gaucho Group Holdings, Inc. | 2024-11-12 | 2024-11-12 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1559998/000149315224044836/form8-k.htm) |
| Danimer Scientific, Inc. | 2025-03-18 | 2025-03-18 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1779020/000095017025041367/dnmr-20250318.htm) |
| Kiromic Biopharma, Inc. | 2025-03-21 | 2025-03-21 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/1792581/000143774925008671/krbp20250321_8k.htm) |
| Benson Hill, Inc. | 2025-03-25 | 2025-03-20 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1830210/000183021025000016/bhil-20250319.htm) |
| Gold Flora Corp. | 2025-03-27 | 2025-03-27 | receivership_application | [一手披露](https://www.sec.gov/Archives/edgar/data/1876945/000187694525000018/gram-20250328.htm) |
| Global Clean Energy Holdings, Inc. | 2025-04-18 | 2025-04-16 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/748790/000121390025074671/ea0251950-1512g_global.htm) |
| Molecular Templates, Inc. | 2025-04-24 | 2025-04-20 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1183765/000164117225006026/form8-k.htm) |
| WW INTERNATIONAL, INC. | 2025-05-06 | 2025-05-06 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/105319/000119312525268065/ww-20250930.htm) |
| Accelerate Diagnostics, Inc | 2025-05-08 | 2025-05-08 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/727207/000110465925046104/tm2514448d1_8k.htm) |
| Li-Cycle Holdings Corp. | 2025-05-15 | 2025-05-14 | ccaa_restructuring | [一手披露](https://www.sec.gov/Archives/edgar/data/1828811/000119312525122542/d892872d8k.htm) |
| Arch Therapeutics, Inc. | 2025-05-23 | 2025-04-18 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1537561/000164117225012288/form8-k.htm) |
| iCoreConnect Inc. | 2025-06-03 | 2025-06-02 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1906133/000147793225004389/icct_8k.htm) |
| 4Front Ventures Corp. | 2025-06-10 | 2025-06-09 | canadian_bankruptcy_assignment | [一手披露](https://www.sec.gov/Archives/edgar/data/1783875/000127956925000595/form8k.htm) |
| MARIN SOFTWARE INC | 2025-07-03 | 2025-07-01 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1389002/000095017025093994/mrin-20250630.htm) |
| Wag! Group Co. | 2025-07-21 | 2025-07-21 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1842356/000184235625000098/pet-20250829.htm) |
| LadRx Corp | 2025-07-31 | 2025-07-28 | assignment_for_benefit_of_creditors | [一手披露](https://www.sec.gov/Archives/edgar/data/799698/000164117225021728/form8-k.htm) |
| DIAMONDHEAD CASINO CORP | 2025-08-04 | 2025-07-31 | involuntary_chapter_7_order_for_relief | [一手披露](https://www.sec.gov/Archives/edgar/data/844887/000164117225021981/form8-k.htm) |
| TPI COMPOSITES, INC | 2025-08-11 | 2025-08-11 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1455684/000117184325005450/f8k_081525.htm) |
| ModivCare Inc | 2025-08-21 | 2025-08-20 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1220754/000143774925038831/modv20251226_8k.htm) |
| LUXURBAN HOTELS INC. | 2025-09-18 | 2025-09-14 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1893311/000182912625007474/luxurban_8k.htm) |
| Loop Media, Inc. | 2025-10-20 | 2025-10-09 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/1643988/000149315225018533/form8-k.htm) |
| FORMER BL STORES INC | 2025-10-30 | 2024-09-09 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/768835/000076883525000034/big-20251024.htm) |
| OFFICE PROPERTIES INCOME TRUST | 2025-10-31 | 2025-10-30 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1456772/000110465925104631/tm2529875d1_8k.htm) |
| TILT Holdings Inc. | 2025-11-07 | 2025-11-07 | ccaa_restructuring | [一手披露](https://www.sec.gov/Archives/edgar/data/1761510/000110465925108692/tilt-20251103xex10d16.htm) |
| Sonder Holdings Inc. | 2025-11-10 | 2025-11-14 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/1819395/000162828025052329/son-20251107.htm) |
| Clearside Biomedical, Inc. | 2025-11-25 | 2025-11-23 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1539029/000119312525332152/d63805dex991.htm) |
| Lazydays Holdings, Inc. | 2025-12-01 | 2025-11-28 | assignment_for_benefit_of_creditors | [一手披露](https://www.sec.gov/Archives/edgar/data/1721741/000149315225025447/ex10-1.htm) |
| IROBOT CORP | 2025-12-15 | 2025-12-14 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1159167/000119312525318337/d97115d8k.htm) |
| Luminar Technologies, Inc./DE | 2025-12-15 | 2025-12-15 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1758057/000114036125045494/ef20061234_8k.htm) |
| ZYNEX INC | 2025-12-16 | 2025-12-15 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/846475/000110465926009241/zyxi-20260129x8k.htm) |
| DYNATRONICS CORP | 2026-01-12 | 2026-01-09 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/720875/000106299326000178/form8k.htm) |
| Twin Hospitality Group Inc. | 2026-01-27 | 2026-01-26 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/2011954/000149315226003699/form8-k.htm) |
| Fat Brands, Inc | 2026-01-27 | 2026-01-26 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1705012/000149315226003696/form8-k.htm) |
| Nine Energy Service, Inc. | 2026-02-02 | 2026-02-01 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1532286/000121390026025721/ea0280259-8k_nineenergy.htm) |
| CHARLES & COLVARD LTD | 2026-03-04 | 2026-03-02 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1015155/000110465926023038/tm267752d2_8k.htm) |
| CUMULUS MEDIA INC | 2026-03-05 | 2026-03-04 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1058623/000110465926023855/tm267873d1_8k.htm) |
| Cannabist Co Holdings Inc. | 2026-03-24 | 2026-03-24 | ccaa_restructuring | [一手披露](https://www.sec.gov/Archives/edgar/data/1776738/000114036126010926/ef20068552_8k.htm) |
| Broad Street Realty, Inc. | 2026-03-26 | 2026-03-20 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/764897/000119312526126180/d53205d8k.htm) |
| Iterum Therapeutics plc | 2026-03-27 | 2026-03-27 | irish_winding_up_petition | [一手披露](https://www.sec.gov/Archives/edgar/data/1659323/000119312526127785/itrm-20260327.htm) |
| LIPELLA PHARMACEUTICALS INC. | 2026-03-31 | 2026-03-30 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1347242/000175392626001173/g085807_8k.htm) |
| IO Biotech, Inc. | 2026-03-31 | 2026-03-31 | chapter_7 | [一手披露](https://www.sec.gov/Archives/edgar/data/1865494/000119312526133331/d87973d8k.htm) |
| QVC Group, Inc. | 2026-04-17 | 2026-04-16 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1355096/000110465926086686/tm2621254d1_8k.htm) |
| OLENOX INDUSTRIES INC. | 2026-05-04 | 2026-04-28 | subsidiary_chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1023994/000121390026051696/ea0289100-8k_olenox.htm) |
| SOCIETY PASS INCORPORATED. | 2026-05-14 | 2026-05-12 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1817511/000149315226023162/form8-k.htm) |
| Bitcoin Depot Inc. | 2026-05-18 | 2026-05-17 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1901799/000119312526227832/d130621d8k.htm) |
| Trinseo PLC | 2026-05-26 | 2026-05-26 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1519061/000110465926091910/tse-20260630x10q.htm) |
| Inotiv, Inc. | 2026-06-03 | 2026-06-03 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/720154/000162828026048369/notv-20260714.htm) |
| GoHealth, Inc. | 2026-06-08 | 2026-06-07 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1808220/000162828026041369/goco-20260605.htm) |
| Sleep Number Corp | 2026-06-12 | 2026-06-12 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/827187/000082718726000053/snbr-20260616.htm) |
| SANGAMO THERAPEUTICS, INC | 2026-06-23 | 2026-06-23 | chapter_11 | [一手披露](https://www.sec.gov/Archives/edgar/data/1001233/000119312526278582/d161847d8k.htm) |

## 24 个负样本缺口

这些窗口没有指定完整随访区间与要排除的事件集合。当前选读的公司报告、后续融资披露和搜索结果不足以证明整个区间没有违约、债务交换或评级变化，也不能证明低风险。各公司保留检索来源及具体问题。

| 公司 | 本轮审阅发现/边界 |
|---|---|
| United Airlines Holdings, Inc. | Selected annual disclosure concerns financing; Azul bankruptcy hits are a different issuer. |
| JETBLUE AIRWAYS CORP | Financing and liquidity covenants do not establish absence of credit events. |
| Frontier Group Holdings, Inc. | Reported year-end liquidity does not certify the entire observation window. |
| Tesla, Inc. | Liquidity forecasts and annual filings are not exhaustive event follow-up. |
| Rivian Automotive, Inc. / DE | Debt and DOE financing disclosed; borrowing capacity is conditional, not a low-risk label. |
| Lucid Group, Inc. | Selected loan drawdowns occurred after the original window; cannot certify the earlier window. |
| Macy's, Inc. | April 2025 ABL amendment is outside the February window endpoint. |
| KOHLS Corp | Restructuring charges and debt repayment must not be equated with a bankruptcy determination. |
| Wayfair Inc. | Debt management disclosures need event and date review; no certified negative. |
| PFIZER INC | Generic indenture default clauses describe conditions, not occurred defaults. |
| Moderna, Inc. | September 2026 financing is after the March window endpoint; other issuer litigation hits excluded. |
| BioNTech SE | BioNTech and Arbutus must be distinguished; liquidity report does not certify absence of events. |
| Sunrun Inc. | Asset-level non-recourse financing differs from parent credit; no complete follow-up. |
| PLUG POWER INC | Material financing risks remain; no bankruptcy finding is not proof of low risk. |
| Sinclair, Inc. | Refinancing is a credit event even if no bankruptcy; scope needs follow-up. |
| iHeartMedia, Inc. | Debt exchange history prevents interpreting a null bankruptcy label as no credit event. |
| Wendy's Co | Hypothetical covenant default clauses are not actual default findings. |
| Dine Brands Global, Inc. | Securitization subsidiaries differ from parent; refinancing outside original window. |
| MARA Holdings, Inc. | Digital assets and borrowing do not certify stable liquidity throughout window. |
| Hilton Worldwide Holdings Inc. | Hotel owner defaults are not automatically Hilton Worldwide defaults; wrong CIK hits excluded. |
| Airbnb, Inc. | Sonder insolvency hits are not Airbnb; later Airbnb filing cannot certify earlier period. |
| AMC ENTERTAINMENT HOLDINGS, INC. | Refinancing and liquidity risks coexist; null event label is not a low-risk label. |
| GameStop Corp. | August 2026 exchange is outside original May endpoint; no exhaustive prior follow-up. |
| BEYOND MEAT, INC. | Debt covenants and later financing changes need interval-specific review. |

## 验证与重建

13 项数据集与公司清单测试通过。另验证发布文件哈希、100 家 CIK、事件前窗口截止日、材料公开日边界及标签不进入观察输入。未运行付费模型批处理。

```sh
python projects/credit_risk/dataset/builders/build_r10_expand.py \
  --discovery projects/credit_risk/dataset/expansion/web_sources.json \
  --evidence projects/credit_risk/dataset/expansion/review_backfill.json \
  --audit projects/credit_risk/dataset/review/outcome_audit_2026-09-09.json \
  --output projects/credit_risk/dataset/expansion/releases/NEW_VERSION
```
