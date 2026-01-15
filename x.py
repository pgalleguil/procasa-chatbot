from api_leads_intelligence import get_leads_executive_report

data = get_leads_executive_report()

print(data["kpis"])
print(data["leads"][:10])
