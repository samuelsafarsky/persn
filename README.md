# ENEX CRZ Research MVP

Appka bezi takto: zadas okres a system automaticky spravi CRZ research.

## Co robi
- automaticky stiahne posledny dostupny CRZ export (`https://www.crz.gov.sk/export/YYYY-MM-DD.zip`)
- najde zmluvy pre elektrinu a plyn
- vyfiltruje obce/mesta v zadanom okrese
- skusi vytiahnut jednotkovu cenu (EUR/MWh) zo zmluvnych textov
- porovna ju s benchmarkom:
  - elektrina: `112.5 EUR/MWh`
  - plyn: `29 EUR/MWh`
- vytvori hotovy predmet + komplet telo emailu pre kazdy najdeny subjekt
- vypocita odhad uspory za tento mesiac pre kazdy subjekt
- zobrazi live log krokov a pripadne chyby
- umozni zrusit rozbehnuty job cez `Cancel`
- umozni export CSV s komplet konceptami (`/export/<job_id>.csv`)

## Instalacia
```powershell
cd "C:\Users\esyst\Documents\New project\enex_crz_email_mvp"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Spustenie
```powershell
python app.py
```
Otvor v prehliadaci:
- http://127.0.0.1:5055

## Vstup
- `Okres` (napr. `Senica`)
- volitelne `CRZ ZIP`, ak automaticke stiahnutie zlyha (napr. firewall/proxy blokuje CRZ)

## Vystup
- priebezny stav jobu + logy
- error spravy v UI, ked sa nieco pokazi
- tabulka zmluv s cenou, benchmarkom a rozdielom
- komplet koncepty emailov pre vsetky najdene subjekty
- odhad uspory za tento mesiac po subjektoch aj sumarne za okres

## Limity
- CRZ data nemaju vzdy jednotkovu cenu v rovnakom tvare, preto je extrakcia heuristicka.
- Ak nie je jednotkova cena, pouzije sa fallback odhad (12 % z mesacneho podielu hodnoty zmluvy).
- Ak sa meno kontaktu nenajde v metadatach zmluvy, koncept pouzije neutralny fallback.
