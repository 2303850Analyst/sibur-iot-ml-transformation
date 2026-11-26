"""Build the README figures from the frozen result files. Run with matplotlib and openpyxl."""
import csv
from pathlib import Path
from statistics import mean

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from openpyxl import load_workbook

root = Path(__file__).resolve().parents[2]
reports = root / "writing" / "Финальная оценка Ex"
figures = root / "docs" / "figures"
figures.mkdir(exist_ok=True, parents=True)
plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False,
                     "axes.spines.right": False, "font.size": 11, "axes.axisbelow": True})
def read_csv(name):
    with (reports / name).open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))
def number(s):
    return float(s.replace(",", "."))
def fmt(x):
    return f"{x:.1f}".replace(".", ",")

manual = next(r for r in read_csv("As-is ПСВ - подбор ручных длительностей.csv") if r["Сценарий"] == "Выбранный As-is 5.2")
paired = [r for r in read_csv("As-is ПСВ - закрытие по суткам.csv") if r["Сценарий"] == "Цифровая приёмка при том же выпуске"]
assert len(paired) == 90 and all(int(r["Из них закрыто в свои сутки"]) == 64 for r in paired)
manual_stats = [number(manual["Среднее P25 по завершённым, мин"]),number(manual["P95 по завершённым, мин"])]
digital_stats = [mean(number(r["Среднее P25 за 30 суток по завершённым, мин"]) for r in paired),
                 max(number(r["P95 за 30 суток по завершённым, мин"]) for r in paired)]
fig, ax = plt.subplots(figsize=(9, 3.55), layout="constrained")
for values, dy, color, label in [(manual_stats,-.18,"#43556B","Ручная приёмка"),(digital_stats,.18,"#087764","Цифровая приёмка + 1 Zhengdu")]:
    ax.barh([0+dy,1+dy], values, height=.30, color=color, label=label)
    for i, v in enumerate(values):
        ax.text(v+.8,i+dy,fmt(v),va="center",color=color,weight="bold")
ax.set(yticks=[0,1],yticklabels=["Среднее","P95"],xlim=(0,74),xlabel="От выпуска первой паллеты до P25, минуты")
ax.invert_yaxis()
ax.grid(axis="x",alpha=.18)
ax.set_title("Время поставки при одинаковом графике выпуска", loc="left",weight="bold",pad=12)
ax.legend(loc="lower center",bbox_to_anchor=(.5,-.46),ncol=2,frameon=False)
fig.savefig(figures/"paired-process.png",dpi=155,bbox_inches="tight")
plt.close(fig)

wb=load_workbook(reports/"Приёмка ПСВ - 148 парков без зарядных пауз.xlsx",read_only=True,data_only=True)
sheet=wb["Выбор парка"]
fig,axes=plt.subplots(1,3,figsize=(12,4.3),sharey=True,layout="constrained")
for ax,row in zip(axes,sheet.iter_rows(min_row=41,max_row=43,max_col=7,values_only=True)):
    load,code,pay,loss,op,initial,npv=row
    assert abs(pay+loss+op+initial-npv)<1e-6
    cumulative=0.
    for k,v in enumerate([pay,loss,op,initial]):
        nxt=cumulative+v
        ax.bar(k,abs(v),bottom=min(nxt,cumulative),width=.68,color="#087764" if v>=0 else "#B05C45")
        ax.plot([k+.34,k+.66],[nxt,nxt],color="#78838C",lw=.9)
        ax.text(k,max(cumulative,nxt)+.65,("+" if v>0 else "")+fmt(v),ha="center",fontsize=9)
        cumulative=nxt
    ax.bar(4,npv,width=.68,color="#334E68")
    ax.text(4,npv+(.85 if npv>0 else -1.75),fmt(npv),ha="center",weight="bold",color="#253B50")
    ax.set_title(f"{load} паллет/сутки",loc="left",weight="bold")
    ax.set_xticks(range(5),["Тариф","Потери","OPEX","CAPEX","NPV"],rotation=20)
    ax.axhline(0,color="#45515C",lw=.8)
    ax.grid(axis="y",alpha=.17)
axes[0].set_ylabel("Приведённая стоимость за 5 лет, млн ₽")
axes[0].set_ylim(-8,32)
fig.suptitle("Из чего складывается NPV выбранного парка",weight="bold",fontsize=16)
fig.savefig(figures/"npv-contribution.png",dpi=155,bbox_inches="tight")
plt.close(fig)

