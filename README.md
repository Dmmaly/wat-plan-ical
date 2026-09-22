# wat-plan-ical

Plan zajęć WEL WAT (HTML) → plik ICS → subskrypcja w Kalendarzu iPhone. Aktualizacja co tydzień przez GitHub Actions.

```bash
pip install -r requirements.txt
python src/wat_plan_to_ics.py                                   # pobiera zima+lato, zapisuje docs/plan.ics
python src/wat_plan_to_ics.py --from-file strona.htm --out test.ics   # test offline
```

Subskrypcja na iPhonie: `webcal://dmmaly.github.io/wat-plan-ical/plan.ics`

- `src/wat_plan_to_ics.py` – parser HTML -> ICS
- `.github/workflows/update-calendar.yml` – aktualizacja co poniedziałek
- `docs/plan.ics` – kalendarz publikowany przez GitHub Pages
