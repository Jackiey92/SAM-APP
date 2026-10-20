"""Author fixture before any retrieval; never edit golden based on results."""
import json
from pathlib import Path
metrics = [('营业收入','1.2亿元'),('净利润','1800万元'),('毛利率','32%'),('前五大客户收入占比','65%'),('应收账款余额','2400万元'),('研发费用','900万元'),('融资投后估值','3亿元')]
rows=[]
for period in ['2023全年','2024全年','2024上半年']:
    for subject in ['星河科技合并报表','星河科技母公司']:
        for metric, value in metrics:
            rows.append({'id':len(rows)+1,'text':f'{subject}在{period}的{metric}为{value}。','tricky':True})
extras=['星河科技2024全年销售回款1.2亿元，不等于营业收入。','星河科技2024全年未发生新增股权融资。','星河科技子公司云帆软件2024全年营业收入为12000万元。','客户海蓝集团2024全年采购星河科技产品金额为3000万元。','星河科技2023全年销售回款为9000万元。','星河科技2024上半年未确认对客户海蓝集团的营业收入。']
for t in extras: rows.append({'id':len(rows)+1,'text':t,'tricky':True})
queries=[]
for period,subject,metric in [('2023全年','合并报表','营业收入'),('2024全年','合并报表','营业收入'),('2024上半年','合并报表','营业收入'),('2024全年','母公司','营业收入'),('2024全年','合并报表','净利润'),('2023全年','母公司','净利润'),('2024上半年','母公司','毛利率'),('2024全年','合并报表','前五大客户收入占比'),('2023全年','合并报表','应收账款余额'),('2024全年','母公司','研发费用'),('2024上半年','合并报表','融资投后估值')]:
    gold=[r['id'] for r in rows if f'星河科技{subject}在{period}的{metric}为' in r['text']]
    queries.append({'query':f'星河科技{period}{subject}的{metric}是多少？','golden':gold,'tricky':True})
for q,g in [('星河科技2024年全年实际收到的销售款是多少？',43),('星河科技2024年有没有新增股权融资？',44),('云帆软件2024全年收入多少万元？',45),('海蓝集团2024年向星河科技采购了多少钱？',46),('星河科技2023年的回款是多少？',47),('2024上半年星河科技是否确认了海蓝集团收入？',48)]: queries.append({'query':q,'golden':[g],'tricky':True})
Path(__file__).with_name('recall_fixtures.json').write_text(json.dumps({'company_id':'acc-m0','facts':rows,'queries':queries},ensure_ascii=False,indent=2))
