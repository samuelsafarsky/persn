# ENEX CRZ Email MVP

Tento systém automatizuje tvorbu personalizovaných emailových konceptov pre obce na základe údajov z Centrálneho registra zmlúv (CRZ). Je navrhnutý ako rýchly lokálny nástroj pre obchodných zástupcov ENEX.

## Hlavné funkcie
- **Rýchly Discover:** Okamžité vyhľadanie obcí v okrese pomocou predgenerovanej lokálnej mapovacej databázy.
- **Robustná extrakcia cien:** Pokročilé regulárne výrazy a kontextové vyhodnocovanie pre získanie jednotkových cien (EUR/MWh) priamo z textov zmlúv.
- **Inteligentné odhady:** Výpočet úspor s priradenou úrovňou dôveryhodnosti (Confidence Level) a označením použitej metódy.
- **Slovenčina a ASCII:** UI a emaily sú v slovenčine s plnou podporou diakritiky.
- **Live log:** Sledovanie priebehu spracovania v reálnom čase (sťahovanie, parsovanie XML, filtrovanie).
- **Export:** Možnosť stiahnuť všetky vygenerované koncepty vo formáte CSV.

## Technický Stack
- **Backend:** Python + Flask
- **Dáta:** pandas pre analytiku, xml.etree pre parsovanie CRZ exportov
- **Frontend:** HTML5, CSS3 (Tailwind-like styling), Vanilla JS

## Inštalácia a spustenie

1. **Príprava prostredia:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Spustenie aplikácie:**
   ```bash
   python app.py
   ```
   Aplikácia beží na: `http://127.0.0.1:5055`

## Workflow
1. **Zadanie okresu:** Zadajte názov okresu (napr. "Senica").
2. **Nájdenie obcí:** Kliknite na "Nájsť obce v CRZ". Systém okamžite zobrazí zoznam obcí.
3. **Výber obcí:** Označte obce, ktoré chcete spracovať (možnosť "Označiť všetko").
4. **Spracovanie:** Kliknite na "Spracovať vybrané obce". Systém začne sťahovať a analyzovať dáta z CRZ (lookback 180 dní).
5. **Výsledky:** Po dokončení sa zobrazí sumárny odhad úspor, tabuľka výsledkov a vygenerované emailové koncepty.

## Poznámky k dátam
- **Benchmarky:** Elektrina: 112.5 €/MWh, Plyn: 29 €/MWh.
- **Confidence Levels:**
  - `Vysoká`: Nájdená jednotková cena aj celková hodnota zmluvy.
  - `Stredná`: Nájdená jednotková cena alebo odhad z celkovej sumy.
  - `Nízka`: Modelový odhad (12% z benchmarku) pri nedostatku presných dát.
- **Cache:** ZIP súbory z CRZ sa ukladajú do `data/cache/` pre rýchlejšie opakované spustenie.
