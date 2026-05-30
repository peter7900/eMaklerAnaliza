# Przykłady użycia

## Instalacja zależności

```bash
cd /ścieżka/do/emakler_analiza1
python3 -m venv venv
source venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

## Raport portfela na wybraną datę

```bash
python -m portfolio --csv eMAKLER_historia_zlecen1.Csv --date 2025-06-15
```

Wynik na stdout (ostrzeżenia i pominięcia na stderr).

Uwaga: przy podaniu `--csv` transakcje są importowane do bazy SQLite (domyślnie `./portfolio.sqlite`) i kolejne obliczenia wykonywane są na danych z bazy.

Domyślnie włączony jest cache SQLite w bazie `./portfolio.sqlite` (sekcja poniżej).

## Import do bazy (tylko import)

```bash
python -m portfolio --csv eMAKLER_historia_zlecen1.Csv --import-only
```

Domyślna baza to `./portfolio.sqlite` (można zmienić parametrem `--db`). Podczas importu zapisywane są tylko transakcje, których hash nie istnieje jeszcze w bazie.

## Raport zbiorczy P/L za całą historię (bez `--date`)

```bash
python -m portfolio --csv eMAKLER_historia_zlecen1.Csv
```

Tryb ten generuje raport P/L per papier dla całego okresu (zrealizowany + niezrealizowany) i obsługuje również `--plot`.

Dodatkowe kolumny (opcjonalnie):

```bash
python -m portfolio --csv eMAKLER_historia_zlecen1.Csv --pct --comm
```

- `--pct` dodaje kolumnę P/L %
- `--comm` dodaje kolumnę prowizji (PLN)

## Uruchomienie bez pliku CSV (tylko na danych z bazy)

```bash
python -m portfolio --pct --comm
```

## Statystyki bazy

```bash
python -m portfolio --stats
```

## Własny plik mapowań instrumentów

```bash
python -m portfolio --csv moj_export.csv --date 2025-09-30 --instruments /ścieżka/instruments.yaml
```

Bez `--instruments` używane jest domyślne `instruments.yaml` w katalogu projektu.

## Cache SQLite (kursy NBP i wycena Yahoo)

Program zapisuje pobrane z sieci dane (NBP, Yahoo) w **tej samej bazie** co transakcje, tj. domyślnie w `./portfolio.sqlite` (lub w pliku wskazanym parametrem `--db`).

## Wymuszenie pobrania z sieci (`--no-cache`)

```bash
python -m portfolio --csv moj_export.csv --date 2025-09-30 --no-cache
```

Opcja **pomija odczyt** z cache i przed pobraniem **usuwa** z bazy wpisy dla używanych w danym uruchomieniu kluczy, żeby uniknąć pozostawienia przestarzałej wartości przy nieudanym żądaniu. Po udanym pobraniu dane są z powrotem zapisywane do bazy `portfolio.sqlite`.

Można ją łączyć z innymi parametrami, np.:

```bash
python -m portfolio --csv moj_export.csv --date 2025-09-30 --instruments /ścieżka/instruments.yaml --no-cache
```

Możesz też wskazać bazę:

```bash
python -m portfolio --db /ścieżka/portfolio.sqlite --csv moj_export.csv --date 2025-09-30 --no-cache
```

## Szybkie wywołanie z venv w projekcie

```bash
./venv/bin/python -m portfolio --csv eMAKLER_historia_zlecen1.Csv --date 2025-04-18
```

## Format daty


Parametr `--date` (opcjonalny) ma postać `YYYY-MM-DD` (np. `2025-12-31`).
