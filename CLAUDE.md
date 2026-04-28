# Port `claude-desktop-buddy` ze StickC Plus na M5Stack Core2

## Git workflow — git-flow

Repo używa git-flow. Reguły dla agenta i dla człowieka:

- **`main`** — tylko stan release-ready. Każdy merge na `main` reprezentuje wersję którą da się zflashować i używać. Tag-uje się release'y (np. `v0.1-core2-mvp`).
- **`develop`** — integracja codziennej pracy. Domyślny branch dla każdej zmiany która nie jest hotfixem.
- **`feature/<krótki-opis>`** — pojedynczy feature lub krok migracji. Branchowane z `develop`, mergowane z powrotem do `develop` przez PR (squash albo merge commit — do uzgodnienia per zmiana).
- **`release/<wersja>`** — przygotowanie konkretnego release'u (bump wersji, changelog, ostatnie testy). Branchowane z `develop`, po zakończeniu mergowane do `main` (z tagiem) i do `develop`.
- **`hotfix/<krótki-opis>`** — pilna poprawka stanu produkcyjnego. Branchowana z `main`, mergowana do `main` + `develop`.

**Reguły żelazne:**
- Nie commituj bezpośrednio na `main` ani na `develop`. Zawsze przez `feature/`, `release/` lub `hotfix/` branch i PR.
- Commit message: imperatyw, krótki tytuł (≤70 znaków). W body wyjaśnij **co i dlaczego**, nie *jak* (jak widać w diff). Wzorzec — historia migracji StickC→Core2 (commity `Step 1`–`Step 8b`).
- Branch z `feature/` po mergu kasować (`git push origin --delete feature/foo`).

## Cel

Sportować firmware z [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy) z M5StickC Plus (ESP32 + 135×240 portrait + 2 fizyczne przyciski) na **M5Stack Core2** (ESP32 + 320×240 landscape + dotykowy ekran + IMU + głośnik + RTC + bateria), zachowując pełną funkcjonalność: BLE bridge, 18 ASCII pets z 7 animacjami każdy, GIF packs, state machine, wszystkie ekrany (Normal/Pet/Info/Approval/Menu).

## Co się NIE zmienia

- **Wire protocol BLE** (Nordic UART Service UUIDs, JSON schema) — bez zmian, `ble_bridge.cpp` zostaje
- **Folder push transport** dla GIF packs — bez zmian, `xfer.h`, `character.cpp` zostają
- **Logika state machine** w `main.cpp` — bez zmian (states: sleep/idle/busy/attention/celebrate/dizzy/heart, level-up co 50K tokens, auto-screen-off po 30s)
- **NVS-backed stats/settings** — `stats.h` bez zmian
- **18 ASCII species** w `buddies/` — definicje zostają, zmienia się tylko render target (rozmiar/pozycja na ekranie)
- **Manifest format GIF packs** (96px wide, manifest.json structure) — bez zmian po stronie protokołu, ale viewport renderingu na urządzeniu się zwiększa

## Co się ZMIENIA

### 1. Target i biblioteki

**`platformio.ini`** — zamiana całego pliku:

```ini
[env:m5stack-core2]
platform = espressif32
board = m5stack-core2
framework = arduino
monitor_speed = 115200
upload_speed = 1500000
board_build.partitions = default_16MB.csv
board_upload.flash_size = 16MB
board_build.filesystem = spiffs

lib_deps =
    m5stack/M5Unified@^0.2.7
    m5stack/M5GFX@^0.2.7
    bitbank2/AnimatedGIF@^2.1.0
    bblanchon/ArduinoJson@^7.0.0

build_flags =
    -DCORE_DEBUG_LEVEL=2
    -DBOARD_HAS_PSRAM
    -mfix-esp32-psram-cache-issue
```

**Uwaga dla agenta:** `M5StickCPlus` library ZNIKA. Zastępuje ją `M5Unified`, która działa na całej rodzinie M5Stack i ma to samo API dla wszystkich płytek. Wszystkie pliki źródłowe muszą zmienić nagłówek z `#include <M5StickCPlus.h>` na `#include <M5Unified.h>`.

### 2. API mapping (M5StickCPlus → M5Unified)

| Stare API (StickC Plus) | Nowe API (Core2 / M5Unified) | Uwagi |
|---|---|---|
| `M5.begin()` | `M5.begin()` | bez zmian, ale konfiguracja bardziej rozbudowana — użyć `auto cfg = M5.config(); M5.begin(cfg);` |
| `M5.Lcd.*` | `M5.Display.*` | wszystkie wywołania renderingu |
| `M5.Lcd.width()` = 135 | `M5.Display.width()` = 320 | **landscape!** — przeprojektować layouty |
| `M5.Lcd.height()` = 240 | `M5.Display.height()` = 240 | wysokość bez zmian |
| `M5.BtnA.wasPressed()` | **brak fizycznych przycisków** | patrz sekcja 3 — Touch input |
| `M5.BtnB.wasPressed()` | **brak fizycznych przycisków** | patrz sekcja 3 |
| `M5.BtnA.pressedFor(ms)` | symulować przez touch hold detection | patrz sekcja 3 |
| `M5.Imu.getAccelData(...)` | `M5.Imu.getAccel(...)` lub `M5.Imu.update(); auto data = M5.Imu.getImuData();` | inny IMU (MPU6886 → MPU6886 w Core2 v1.0, lub MPU6886 + BMI270 w Core2 v1.1), M5Unified abstrahuje |
| `M5.Axp.SetLed(state)` | brak LED jak w Sticku | patrz sekcja 4 — Attention indicator |
| `M5.Axp.SetSleep()` / `PowerOff()` | `M5.Power.powerOff()` / `M5.Power.deepSleep()` | inny PMU (AXP192 w obu, ale różne funkcje wystawione) |
| `M5.Axp.GetBatVoltage()` | `M5.Power.getBatteryLevel()` (zwraca %) | uproszczone API |
| przyciski Power side | `M5.Power.getKeyState()` lub touch BtnPWR | Core2 ma touch button "C" w lewym dolnym rogu jako power |

### 3. Input layer — **NAJWIĘKSZA ZMIANA**

Core2 nie ma fizycznych A/B/Power. Zamiast tego ma:
- **3 strefy dotykowe pod ekranem** (poniżej widocznego LCD, na grafice obudowy): A | B | C
- **Cały ekran 320×240 jest dotykowy** (capacitive touch via FT6336)

#### Strategia: dwa tryby input

**Tryb A — wykorzystać fizyczne touch buttons pod ekranem (rekomendowane)**

M5Unified eksponuje je jako `M5.BtnA`, `M5.BtnB`, `M5.BtnC` — to są te same nazwy obiektów co w StickC, więc **logika może zostać prawie bez zmian**:

```cpp
M5.update();
if (M5.BtnA.wasPressed()) { /* approve / next screen */ }
if (M5.BtnB.wasPressed()) { /* deny / scroll */ }
if (M5.BtnC.wasPressed()) { /* nowy przycisk — można użyć jako shortcut do menu */ }
if (M5.BtnA.pressedFor(800)) { /* hold A → menu */ }
```

**WAŻNE dla agenta:** na Core2 v1.0 te touch buttons **nie zawsze działają out-of-the-box** — strefy dotyku nie schodzą poniżej krawędzi LCD. Trzeba albo:
- użyć v1.1 (Core2 z 2024+, sprzedawany teraz w Botlandzie/Kamami)
- albo ręcznie obsłużyć dolne ~30px ekranu jako 3 wirtualne strefy A/B/C

#### Mapowanie (Controls table z README):

| Akcja | StickC Plus | Core2 |
|---|---|---|
| approve / next screen | A (front) | BtnA (lewa dolna strefa pod ekranem) |
| deny / scroll | B (right) | BtnB (środkowa dolna strefa) |
| menu | Hold A | Hold BtnA (~800ms) |
| toggle screen | Power short | BtnC (prawa dolna strefa) |
| hard power off | Power 6s | Hold BtnC 6s + obsługa w `M5.Power` |
| shake → dizzy | IMU MPU6886 | IMU MPU6886/BMI270, M5Unified abstrahuje — `M5.Imu.getAccel()` |
| face-down → nap | IMU | identycznie, próg accel.z < -0.8 |

### 4. Attention indicator (LED → ekran/wibracja)

StickC Plus ma czerwoną LED którą buddy miga przy `attention`. Core2 **nie ma takiej LED**, ale ma:
- **Vibration motor** — wbudowany — `M5.Power.setVibration(intensity)` (0-255)
- **Speaker** — można pikać
- **Większy ekran** — można migać tłem albo dodać pulsujący border

**Rekomendacja:** w stanie `attention` puls wibracji (np. 2 krótkie wibracje co 3 sekundy) + migający czerwony border na ekranie. Wibracja jest lepsza niż LED — czujesz buddy'ego nawet jak nie patrzysz na ekran.

### 5. Display layout — przeprojektować pod 320×240 landscape

Oryginał: 135×240 portrait. Pety renderowane jako sprite 96px wide, max ~140px tall.

Core2: 320×240 landscape. **Więcej miejsca poziomo niż pionowo.**

#### Propozycja layoutu Normal/Pet screen

```
┌──────────────────────────────────────────────┐
│  [stats bar: tokens | level | battery]       │  ~20px
├──────────────────────────────────────────────┤
│                                              │
│         ╔═══════════╗                        │
│         ║   PET     ║   [transcript          │  ~180px
│         ║  sprite   ║    snippet /           │
│         ║ 140×140   ║    status text]        │
│         ╚═══════════╝                        │
│                                              │
├──────────────────────────────────────────────┤
│  [A approve]  [B deny]  [C menu]             │  ~40px (touch hint)
└──────────────────────────────────────────────┘
```

#### Approval screen (najważniejszy UX)

Duże, czytelne. Ekran landscape pozwala pokazać prompt + opcje obok siebie:

```
┌──────────────────────────────────────────────┐
│  ⚠ APPROVAL NEEDED                            │
│                                              │
│  [pet sprite small]   "Run command:           │
│                        rm -rf /tmp/cache      │
│                        in ~/projects/foo?"    │
│                                              │
│  ┌──────────┐         ┌──────────┐           │
│  │ APPROVE  │         │  DENY    │           │
│  │   (A)    │         │   (B)    │           │
│  └──────────┘         └──────────┘           │
└──────────────────────────────────────────────┘
```

#### Asset rescaling

GIF packs są 96px wide. Po zmianie na Core2:
- **Opcja 1 (zalecana):** pety renderować w nowej skali 140-160px wide na środku, wokół zostaje miejsce na transcript/status. Wymaga upscale 96 → 140 (faktor 1.46x). M5GFX wspiera `drawPng/drawGif` z parametrem skali.
- **Opcja 2:** zostawić 96px native i wyśrodkować, prawą stronę użyć na transcript. Mniej wow, ale szybsze i ostrzejsze.
- **Opcja 3:** zaktualizować `tools/prep_character.py` żeby produkował 140px-wide assets dla Core2. Przyjąć że Core2 packs są niekompatybilne ze StickC packs (osobny format).

**Dla MVP — opcja 2.** Później można przerobić.

ASCII pety (`buddies/*.cpp`) — render text-based, font scaling przez `M5.Display.setTextSize()`. Zwiększyć rozmiar fontu odpowiednio do nowego ekranu (np. setTextSize(3-4) zamiast 2).

### 6. Power management

Core2:
- Bateria 390 mAh wbudowana (mała — ~3-5h aktywnego użycia)
- Opcjonalnie M5GO2 Bottom z 500 mAh dodatkowo
- AXP192 PMU jak w Sticku, ale inne API w M5Unified

**Zachowania do zmiany:**

```cpp
// stary StickC
M5.Axp.SetSleep();
M5.Axp.PowerOff();

// nowy Core2 / M5Unified
M5.Power.deepSleep();          // screen off, low power, wake on touch
M5.Power.powerOff();            // hard off
int batPct = M5.Power.getBatteryLevel();  // 0-100
bool isCharging = M5.Power.isCharging();
```

Auto-screen-off po 30s zostaje. Wake na touch (zamiast button press).

### 7. Audio (BONUS — nowa funkcja)

Core2 ma głośnik (NS4168) — nie ma go w StickC. Można dodać:
- Subtelne dźwięki przy approve/deny (krótkie pingi)
- Buddy "speaks" przy `celebrate` (pisk radości)
- Różne dźwięki per pet species

API: `M5.Speaker.tone(freq, duration)` lub `M5.Speaker.playRaw(...)` dla sampli.

**Dla MVP:** pominąć. Dodać w v2.

### 8. Mikrofon (BONUS)

Core2 ma SPM1423 mic. Nie używać w MVP — claude-desktop-buddy jest passive recipient BLE, nie potrzebuje mic.

## Plan migracji — kolejność dla agenta

1. **Setup** — sklonować repo, zaktualizować `platformio.ini` (sekcja 1), `pio run` żeby zobaczyć błędy kompilacji
2. **Globalna zamiana includów** — `M5StickCPlus.h` → `M5Unified.h` we wszystkich plikach `src/`
3. **Refactor display calls** — `M5.Lcd.*` → `M5.Display.*` (sed/agent grep)
4. **Refactor power calls** — `M5.Axp.*` → `M5.Power.*` (sekcja 6)
5. **Refactor IMU calls** — sekcja 2, ostrożnie z różnicami w API
6. **Skompilować** — fix wszystkich błędów kompilacji, na tym etapie firmware powinien się budować
7. **Test podstawowy** — flash, sprawdzić czy ekran działa, czy BLE się wystawia, czy łączy się z Claude desktop
8. **Refactor input** — strefy A/B/C jako BtnA/BtnB/BtnC (sekcja 3). Test mapowania.
9. **Redesign layoutu** — przeprojektować rendering pod 320×240 landscape (sekcja 5). Najpierw Normal screen, potem Approval, potem Pet/Info/Menu.
10. **ASCII pets render** — przeskalować rozmiar fontów i pozycje, sprawdzić wszystkie 18 species × 7 animacji
11. **GIF render** — przetestować z `characters/bufo/`, ewentualnie zaktualizować skalowanie
12. **Attention indicator** — dodać wibrację + migający border (sekcja 4)
13. **Touch UX polish** — visual feedback przy tap, debounce, hold detection dla menu
14. **End-to-end test** — pełna sesja Claude Code z permission promptami, kilka godzin idle, level-up celebration
15. **Battery test** — zmierzyć realny czas pracy na baterii, ewentualnie zoptymalizować deep sleep

## Pliki do modyfikacji (lista dla agenta)

```
platformio.ini             ← przepisać całkowicie (sekcja 1)
src/main.cpp                ← include + display + input + power
src/buddy.cpp               ← include + display + skala fontów
src/buddies/*.cpp           ← include + display, każdy z 18 plików
src/ble_bridge.cpp          ← tylko zmiana include (BLE bez zmian)
src/character.cpp           ← include + display, ewentualnie skala GIF
src/data.h                  ← bez zmian
src/xfer.h                  ← bez zmian
src/stats.h                 ← bez zmian (NVS API się nie zmienia)
tools/prep_character.py     ← opcjonalnie: dodać tryb 140px-wide dla Core2
```

## Sprawdziany — testy które agent powinien wykonać

- [ ] `pio run` kompiluje się bez błędów
- [ ] `pio run -t upload` flashuje
- [ ] Po starcie widać ekran z petem (nie czarny, nie crash)
- [ ] Touch zones A/B reagują (test przez serial log)
- [ ] BLE advertising działa — widać "ClaudeBuddy" w skanerze BLE telefonu
- [ ] Hardware Buddy w Claude desktop łączy się
- [ ] Permission prompt z Claude pokazuje się na ekranie buddy'ego
- [ ] Tap A na ekranie approval → approve action
- [ ] Tap B → deny action
- [ ] Hold BtnA → menu się otwiera
- [ ] Wstrząśnięcie (jeśli Core2 ma IMU — sprawdzić wersję) → dizzy state
- [ ] Postawienie ekranem do dołu → nap state
- [ ] Po 30s bez interakcji → screen off
- [ ] Tap → wake
- [ ] GIF pack drag-and-drop → buddy przełącza się w GIF mode
- [ ] Level up co 50K tokens → celebrate
- [ ] Battery indicator pokazuje realny stan baterii

## Różnice hardware StickC Plus vs Core2 — referencja

| Cecha | StickC Plus | Core2 |
|---|---|---|
| MCU | ESP32-PICO-D4 | ESP32-D0WD-V3 |
| RAM | 520 KB SRAM | 520 KB SRAM + 8 MB PSRAM |
| Flash | 4 MB | 16 MB |
| Display | 1.14" ST7789v2 135×240 portrait | 2.0" ILI9342C 320×240 landscape **touch (FT6336)** |
| Buttons | A (front), B (right), Power (left) | 3 touch zones A/B/C pod ekranem |
| IMU | MPU6886 | MPU6886 (v1.0) lub BMI270 (v1.1) |
| Audio | brak (tylko buzzer) | speaker NS4168 + mic SPM1423 |
| RTC | brak | BM8563 |
| LED | czerwona (AXP) | brak |
| Vibration | brak | tak (wbudowany silniczek) |
| Bateria | 120 mAh | 390 mAh wbudowana, opcjonalnie +500 mAh w M5GO2 Bottom |
| PMU | AXP192 | AXP192 |
| USB | USB-C (CH9102) | USB-C (CH9102) |
| Grove | 1× | 1× (PortA I2C) |
| SD | brak | TF/microSD slot |

## Linki referencyjne dla agenta

- Repo źródłowe: https://github.com/anthropics/claude-desktop-buddy
- M5Unified docs: https://github.com/m5stack/M5Unified
- M5GFX docs: https://github.com/m5stack/M5GFX
- Core2 docs: https://docs.m5stack.com/en/core/core2_v1.1
- Wire protocol BLE: REFERENCE.md w repo (Nordic UART Service)
- Przykład M5Unified Core2: https://github.com/m5stack/M5Unified/tree/master/examples

## Co przekazać w prompcie do agenta

Skopiować ten plik jako `CLAUDE.md` w roocie sklonowanego repo, plus jednorazowy prompt:

> Sportuj firmware claude-desktop-buddy ze StickC Plus na M5Stack Core2.
> Cały plan, mapping API i kolejność zmian są w `CLAUDE.md`.
> Zacznij od kroku 1 (Setup) i pracuj sekwencyjnie. Po każdym kroku
> kompiluj (`pio run`), commituj zmianę z opisowym message, i przechodź dalej.
> Jeśli napotkasz nieoczekiwany problem (np. M5Unified API zmieniło się
> względem dokumentacji), zatrzymaj się i zapytaj zamiast zgadywać.

## Estimate

Realnie dla agenta z dostępem do hardware (możliwość flashowania i testowania):
- Sekcje 1-7 (setup + kompilacja czysto): ~1-2h
- Sekcja 8-10 (input + layout redesign + assets): ~3-4h
- Sekcja 11-15 (testy end-to-end + polish): ~2-3h

**Łącznie: jeden dzień roboty.**

Bez hardware (agent pisze, Ty flashujesz i raportujesz): pomnożyć przez 2-3 ze względu na cykle feedback.
