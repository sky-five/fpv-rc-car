// FPV RC car — car-side firmware (ESP32). See firmware/README.md.
//
// !! This board's USB port is dead: OTA is the ONLY way to flash it. !!
// Firmware 2.x protects itself with bootloader rollback (see "OTA rollback
// protection" below): a new image that can't reach WiFi + OTA reverts to the
// previous one on its own. Keep that section intact in every future version.

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ESP32Servo.h>
#include <Preferences.h>
#include "esp_task_wdt.h"
#include "esp_system.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "secrets.h"      // WiFi networks (+ optional OTA_PASSWORD). Gitignored.

#define FW_VERSION "2.0.0"

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_ADDR 0x3C
#define YELLOW_H 16

#define UDP_PORT 4210
#define FAILSAFE_MS 250
// Gentle failsafe: on a brief dropout, hold the last steering angle and freeze the
// throttle for FAILSAFE_HOLD_MS before snapping to neutral. Rides through RF blips that
// flicker around the failsafe threshold (the "glitch"). Set FAILSAFE_HOLD_LAST 0 for the
// old snap-to-neutral behavior. Worst-case the car coasts on last command for
// FAILSAFE_MS + FAILSAFE_HOLD_MS before neutral.
#define FAILSAFE_HOLD_LAST 1
#define FAILSAFE_HOLD_MS 350

// While WiFi is down, try the NEXT network this often. WiFi.begin() is non-blocking and
// setAutoReconnect(true) keeps retrying the current SSID in between, so a long interval
// just controls how soon we fall over to the backup network. Kept long (12s) so we don't
// hammer WiFi.begin() on a flapping link — that churn could wedge the WiFi driver.
#define WIFI_RETRY_MS 12000

// Task watchdog: if the loop ever hangs (WiFi wedge, deadlock) for this long, the chip
// auto-reboots instead of sitting dead until a manual power cycle. Critical when the
// only way to reflash is OTA.
#define WDT_TIMEOUT_S 4

#define BTN_PIN 0          // onboard BOOT button: tap to toggle the OLED on/off
#define STEER_PIN 16
#define STEER_CENTER_US 1500
#define STEER_RANGE_US 400
#define STEER_REVERSE true

#define ESC_PIN 17
#define ESC_NEUTRAL_US 1500
#define FWD_FULL_US 2000
#define REV_FULL_US 1000
#define REV_SLOW_FACTOR 0.5f
#define REV_FULL_FACTOR 1.0f

#define BRAKE_FULL_US 1100
#define BRAKE_THRESHOLD 50
// Open-loop "ABS" brake pulsing. It's a feel effect only — there's no wheel-speed
// feedback, so it can't actually prevent lockup and isn't needed for motor health.
// Default off = steady linear proportional brake (simpler, easier on the ESC).
#define BRAKE_ABS 0
#define ABS_PERIOD_MS 80
#define ABS_DUTY 0.70f
#define ABS_LIGHT_FRAC 0.40f

#define RVS_TAP_US 1300
#define RVS_TAP_MS 120
#define RVS_GAP_MS 80

#define STEER_SLEW 6000
#define ESC_SLEW 4000

#define TRIM_STEP_US 2
#define TRIM_MAX_US 120
#define TRIM_REPEAT_MS 120
#define TRIM_SAVE_MS 2000
#define TRIM_SHOW_MS 2000

#define FLAG_HB       0x01
#define FLAG_TRIM_L   0x02
#define FLAG_TRIM_R   0x04
#define FLAG_TRIM_RST 0x08

#define TELE_MAGIC 0x5A
#define TELE_MS 100        // 10 Hz telemetry: frees 2.4GHz airtime for control RX (was 50 = 20 Hz)

// mDNS name: the laptop finds the car as "rc-car.local" (rccar/discovery.py).
#define OTA_HOSTNAME "rc-car"
// To require a password for OTA uploads, #define OTA_PASSWORD in secrets.h
// (tools/flash.py reads it from there and passes it to espota automatically).

// OTA rollback protection timings (see the section further down).
#define ROLLBACK_CONFIRM_MS 20000    // new image must run this long with WiFi + OTA up...
#define ROLLBACK_DEADLINE_MS 120000  // ...or it is rolled back this long after boot

enum ScoutPose { SC_SIT, SC_TROT, SC_BRACE, SC_REVERSE };

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
WiFiUDP udp;
Servo steerServo;
Servo esc;
Preferences prefs;

struct __attribute__((packed)) DrivePacket {
  uint8_t magic;
  uint8_t version;
  uint32_t seq;
  int16_t steer;
  int16_t drive;
  uint16_t brake;
  uint16_t handbrake;
  uint8_t gear;
  uint8_t flags;
};

struct __attribute__((packed)) TelemetryPacket {
  uint8_t magic;
  uint8_t version;
  uint32_t seq;
  uint8_t gear;
  uint8_t drvState;
  uint8_t linkLive;
  uint8_t flags;
  int16_t steer;
  int16_t escOff;
  uint16_t brake;
  uint16_t handbrake;
  int16_t trimUs;
  uint16_t pps;
  uint32_t lost;
  int8_t rssi;          // car WiFi RSSI in dBm (negative); 0 = not connected
  uint8_t resetReason;  // esp_reset_reason(): why the chip last booted (watchdog? panic? power-on?)
  uint16_t freeHeapKb;  // free heap in KB — declining over a session = a leak heading for a crash
};

static const uint8_t MAGIC = 0xA5;
static const uint8_t VERSION = 1;        // control (DrivePacket) protocol — must match g29_reader
static const uint8_t TELE_VERSION = 3;   // telemetry protocol — v3 added resetReason + freeHeapKb

enum RvsState { RVS_IDLE, RVS_TAP, RVS_GAP, RVS_ACTIVE };
RvsState rvsState = RVS_IDLE;
uint32_t rvsStateMs = 0;

enum DriveState { DRV_NEUTRAL, DRV_FORWARD, DRV_BRAKING, DRV_BLOCKED, DRV_REVERSE };
DriveState drvState = DRV_NEUTRAL;

DrivePacket lastPkt;
uint32_t lastPktMs = 0;
uint32_t lastSeq = 0;
uint32_t pktCount = 0;
uint32_t lostCount = 0;
uint16_t pktRate = 0;
uint32_t rateWindowMs = 0;
uint8_t rxBuf[64];

int currentSteerUs = STEER_CENTER_US;
int currentEscUs = ESC_NEUTRAL_US;
uint32_t lastSlewMs = 0;

char ipStr[20] = "0.0.0.0";

bool oledOn = true;
bool btnPrev = false;
uint32_t lastBtnMs = 0;
uint32_t lastReconnectMs = 0;
uint32_t wifiDownSinceMs = 0;

uint32_t animClock = 0;
int roadShift = 0;

int steerTrimUs = 0;
bool trimDirty = false;
uint32_t trimChangedMs = 0;
uint32_t trimShownUntil = 0;

IPAddress senderIp;
uint16_t senderPort = 0;
bool haveSender = false;
uint32_t lastTeleMs = 0;
uint32_t teleSeq = 0;

const char* gearStr(uint8_t g) {
  switch (g) {
    case 0: return "N";
    case 6: return "R";
    case 7: return "r2";
    default: return (g >= 1 && g <= 5) ? (const char*[]){"1","2","3","4","5"}[g - 1] : "?";
  }
}

const char* drvStateStr() {
  switch (drvState) {
    case DRV_FORWARD: return "F";
    case DRV_BRAKING: return "B";
    case DRV_BLOCKED: return "x";
    case DRV_REVERSE: return "R";
    default:          return "N";
  }
}

int steerTargetUs(int16_t steer) {
  float s = steer / 1000.0f;
  if (s < -1.0f) s = -1.0f;
  if (s > 1.0f) s = 1.0f;
  if (STEER_REVERSE) s = -s;
  int us = STEER_CENTER_US + steerTrimUs + (int)(s * STEER_RANGE_US);
  if (us < 1000) us = 1000;
  if (us > 2000) us = 2000;
  return us;
}

void updateTrim(uint32_t now, uint8_t flags, bool linkLive) {
  static bool pL = false, pR = false, pRst = false;
  static uint32_t lastStepMs = 0;
  if (!linkLive) { pL = pR = pRst = false; return; }

  bool l   = flags & FLAG_TRIM_L;
  bool r   = flags & FLAG_TRIM_R;
  bool rst = flags & FLAG_TRIM_RST;
  bool changed = false;

  if (rst && !pRst && steerTrimUs != 0) { steerTrimUs = 0; changed = true; }

  bool nudge = false;
  if ((l && !pL) || (r && !pR)) { nudge = true; lastStepMs = now; }
  else if ((l || r) && (now - lastStepMs >= TRIM_REPEAT_MS)) { nudge = true; lastStepMs = now; }
  if (nudge) {
    if (l) steerTrimUs -= TRIM_STEP_US;
    if (r) steerTrimUs += TRIM_STEP_US;
    if (steerTrimUs >  TRIM_MAX_US) steerTrimUs =  TRIM_MAX_US;
    if (steerTrimUs < -TRIM_MAX_US) steerTrimUs = -TRIM_MAX_US;
    changed = true;
  }

  pL = l; pR = r; pRst = rst;
  if (changed) { trimDirty = true; trimChangedMs = now; trimShownUntil = now + TRIM_SHOW_MS; }
}

int forwardOutputUs(int fwd, uint8_t gear) {
  float f = fwd / 1000.0f;
  if (f > 1.0f) f = 1.0f;
  float factor = gear / 5.0f;
  return ESC_NEUTRAL_US + (int)(f * factor * (FWD_FULL_US - ESC_NEUTRAL_US));
}

int brakeOutputUs(uint32_t now, uint16_t brake, uint16_t handbrake) {
  if (handbrake > BRAKE_THRESHOLD) {
    float h = handbrake / 1000.0f;
    if (h > 1.0f) h = 1.0f;
    return ESC_NEUTRAL_US - (int)(h * (ESC_NEUTRAL_US - BRAKE_FULL_US));
  }
  float p = brake / 1000.0f;
  if (p > 1.0f) p = 1.0f;
  int strong = ESC_NEUTRAL_US - (int)(p * (ESC_NEUTRAL_US - BRAKE_FULL_US));
#if BRAKE_ABS
  int light  = ESC_NEUTRAL_US - (int)(p * (ESC_NEUTRAL_US - BRAKE_FULL_US) * ABS_LIGHT_FRAC);
  float phase = (now % ABS_PERIOD_MS) / (float)ABS_PERIOD_MS;
  return (phase < ABS_DUTY) ? strong : light;
#else
  (void)now;
  return strong;   // linear proportional brake
#endif
}

int reverseOutputUs(uint32_t now, uint8_t gear, int16_t drive, uint16_t brake, uint16_t handbrake) {
  if (handbrake > BRAKE_THRESHOLD || brake > BRAKE_THRESHOLD) { rvsState = RVS_IDLE; return ESC_NEUTRAL_US; }
  int revMag = drive < 0 ? -drive : 0;
  float factor = (gear == 7) ? REV_FULL_FACTOR : REV_SLOW_FACTOR;
  switch (rvsState) {
    case RVS_IDLE:
      if (revMag > BRAKE_THRESHOLD) { rvsState = RVS_TAP; rvsStateMs = now; return RVS_TAP_US; }
      return ESC_NEUTRAL_US;
    case RVS_TAP:
      if (now - rvsStateMs >= RVS_TAP_MS) { rvsState = RVS_GAP; rvsStateMs = now; return ESC_NEUTRAL_US; }
      return RVS_TAP_US;
    case RVS_GAP:
      if (now - rvsStateMs >= RVS_GAP_MS) rvsState = RVS_ACTIVE;
      return ESC_NEUTRAL_US;
    case RVS_ACTIVE:
    default: {
      if (revMag <= BRAKE_THRESHOLD) return ESC_NEUTRAL_US;
      float r = revMag / 1000.0f;
      if (r > 1.0f) r = 1.0f;
      return ESC_NEUTRAL_US - (int)(r * factor * (ESC_NEUTRAL_US - REV_FULL_US));
    }
  }
}

int escTargetUs(uint32_t now, uint8_t gear, int16_t drive, uint16_t brake, uint16_t handbrake) {
  if (gear == 6 || gear == 7) {
    drvState = DRV_REVERSE;
    return reverseOutputUs(now, gear, drive, brake, handbrake);
  }
  if (drvState == DRV_REVERSE) {
    drvState = DRV_BLOCKED;
    rvsState = RVS_IDLE;
  }

  bool brakeCmd = (brake > BRAKE_THRESHOLD) || (handbrake > BRAKE_THRESHOLD);
  int fwd = (gear >= 1 && gear <= 5 && drive > 0) ? drive : 0;

  if (fwd > 0) {
    drvState = DRV_FORWARD;
    return forwardOutputUs(fwd, gear);
  }

  if (brakeCmd) {
    if (drvState == DRV_FORWARD || drvState == DRV_BRAKING || drvState == DRV_NEUTRAL) {
      drvState = DRV_BRAKING;
      return brakeOutputUs(now, brake, handbrake);
    }
    return ESC_NEUTRAL_US;
  }

  if (drvState == DRV_BRAKING)      drvState = DRV_BLOCKED;
  else if (drvState == DRV_FORWARD) drvState = DRV_NEUTRAL;
  return ESC_NEUTRAL_US;
}

int slewToward(int cur, int target, int ratePerSec, float dt) {
  int step = (int)(ratePerSec * dt);
  if (step < 1) step = 1;
  if (target > cur) return (cur + step > target) ? target : cur + step;
  if (target < cur) return (cur - step < target) ? target : cur - step;
  return cur;
}

void drawBar(int x, int y, int w, int h, float v, bool centered) {
  display.drawRect(x, y, w, h, SSD1306_WHITE);
  int innerW = w - 2, innerH = h - 2;
  if (centered) {
    int mid = x + w / 2;
    int half = innerW / 2;
    int n = (int)(fabsf(v) * half);
    if (n > half) n = half;
    if (v >= 0) display.fillRect(mid, y + 1, n, innerH, SSD1306_WHITE);
    else        display.fillRect(mid - n, y + 1, n, innerH, SSD1306_WHITE);
    display.drawFastVLine(mid, y - 1, h + 2, SSD1306_WHITE);
  } else {
    int n = (int)(v * innerW);
    if (n > innerW) n = innerW;
    if (n < 0) n = 0;
    display.fillRect(x + 1, y + 1, n, innerH, SSD1306_WHITE);
  }
}

// 4-bar ascending WiFi strength meter from RSSI (dBm). level 0..4.
void drawWifiBars(int x, int baseY, int level) {
  for (int i = 0; i < 4; i++) {
    int h = 2 + i * 2;             // 2,4,6,8
    int bx = x + i * 3;
    int by = baseY - h + 1;
    if (i < level) display.fillRect(bx, by, 2, h, SSD1306_WHITE);
    else           display.drawRect(bx, by, 2, h, SSD1306_WHITE);
  }
}

void drawScout(int bx, uint8_t pose, int legFrame, bool wag, bool fast, bool noLink) {
  switch (pose) {
    case SC_TROT: {
      display.fillTriangle(bx + 9, 46, bx + 4, 40, bx + 7, 42, SSD1306_WHITE);
      display.fillRoundRect(bx + 8, 45, 17, 7, 2, SSD1306_WHITE);
      display.fillRoundRect(bx + 22, 41, 9, 8, 2, SSD1306_WHITE);
      display.fillTriangle(bx + 24, 41, bx + 20, 38, bx + 25, 40, SSD1306_WHITE);
      display.fillRect(bx + 30, 45, 3, 3, SSD1306_WHITE);
      display.fillRect(bx + 32, 48, 2, 2, SSD1306_WHITE);
      display.drawPixel(bx + 28, 43, SSD1306_BLACK);
      if (legFrame == 0) {
        display.fillRect(bx + 23, 52, 2, 3, SSD1306_WHITE);
        display.fillRect(bx + 12, 52, 2, 3, SSD1306_WHITE);
        display.fillRect(bx + 17, 52, 2, 2, SSD1306_WHITE);
      } else {
        display.fillRect(bx + 23, 52, 2, 2, SSD1306_WHITE);
        display.fillRect(bx + 12, 52, 2, 2, SSD1306_WHITE);
        display.fillRect(bx + 18, 52, 2, 3, SSD1306_WHITE);
      }
      if (fast) {
        display.drawLine(bx + 1, 46, bx + 5, 46, SSD1306_WHITE);
        display.drawLine(bx + 1, 49, bx + 4, 49, SSD1306_WHITE);
      }
      break;
    }
    case SC_BRACE: {
      display.fillRoundRect(bx + 11, 46, 15, 6, 2, SSD1306_WHITE);
      display.fillTriangle(bx + 11, 49, bx + 6, 52, bx + 9, 50, SSD1306_WHITE);
      display.fillRoundRect(bx + 23, 42, 9, 8, 2, SSD1306_WHITE);
      display.fillTriangle(bx + 25, 42, bx + 21, 39, bx + 26, 41, SSD1306_WHITE);
      display.fillRect(bx + 31, 46, 3, 2, SSD1306_WHITE);
      display.fillRect(bx + 26, 44, 2, 2, SSD1306_BLACK);
      display.fillRect(bx + 27, 52, 2, 3, SSD1306_WHITE);
      display.fillRect(bx + 24, 52, 2, 3, SSD1306_WHITE);
      display.fillRect(bx + 13, 52, 2, 3, SSD1306_WHITE);
      display.drawLine(bx + 24, 55, bx + 31, 55, SSD1306_WHITE);
      display.fillRect(bx + 18, 36, 1, 4, SSD1306_WHITE);
      display.fillRect(bx + 18, 41, 1, 1, SSD1306_WHITE);
      break;
    }
    case SC_REVERSE: {
      display.fillRoundRect(bx + 13, 46, 14, 6, 2, SSD1306_WHITE);
      display.fillTriangle(bx + 27, 47, bx + 32, 44, bx + 30, 47, SSD1306_WHITE);
      display.fillRoundRect(bx + 9, 42, 9, 8, 2, SSD1306_WHITE);
      display.fillTriangle(bx + 15, 42, bx + 12, 39, bx + 16, 41, SSD1306_WHITE);
      display.fillRect(bx + 7, 46, 3, 2, SSD1306_WHITE);
      display.drawPixel(bx + 12, 45, SSD1306_BLACK);
      display.fillRect(bx + 24, 52, 2, 3, SSD1306_WHITE);
      display.fillRect(bx + 15, 52, 2, 3, SSD1306_WHITE);
      display.setTextSize(1); display.setTextColor(SSD1306_WHITE);
      display.setCursor(bx + 29, 38); display.print("R");
      display.fillTriangle(bx + 30, 46, bx + 34, 44, bx + 34, 48, SSD1306_WHITE);
      display.fillRect(bx + 34, 45, 3, 2, SSD1306_WHITE);
      break;
    }
    case SC_SIT:
    default: {
      if (wag) display.fillTriangle(bx + 13, 46, bx + 8, 43, bx + 10, 46, SSD1306_WHITE);
      else     display.fillTriangle(bx + 13, 48, bx + 8, 50, bx + 10, 48, SSD1306_WHITE);
      display.fillRoundRect(bx + 12, 48, 9, 6, 2, SSD1306_WHITE);
      display.fillRoundRect(bx + 15, 42, 9, 10, 3, SSD1306_WHITE);
      display.fillRoundRect(bx + 19, 38, 9, 8, 2, SSD1306_WHITE);
      display.fillTriangle(bx + 20, 38, bx + 19, 33, bx + 23, 37, SSD1306_WHITE);
      display.fillRect(bx + 27, 42, 3, 2, SSD1306_WHITE);
      display.drawPixel(bx + 25, 41, SSD1306_BLACK);
      display.fillRect(bx + 21, 50, 2, 4, SSD1306_WHITE);
      display.fillRect(bx + 24, 50, 2, 4, SSD1306_WHITE);
      if (noLink) { display.setTextSize(1); display.setTextColor(SSD1306_WHITE);
                    display.setCursor(bx + 30, 36); display.print("?"); }
      break;
    }
  }
}

void drawStatus(bool linkLive) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  float strV = lastPkt.steer / 1000.0f;
  float thrV = (currentEscUs - ESC_NEUTRAL_US) / 500.0f;
  float brkV = lastPkt.brake / 1000.0f;
  float hbV  = lastPkt.handbrake / 1000.0f;

  bool reverse = (lastPkt.gear == 6 || lastPkt.gear == 7);

  animClock++;
  int spd = (int)(fabsf(thrV) * 3.0f + 0.5f);
  if (spd > 3) spd = 3;
  int dir = 0;
  if (linkLive) {
    if (reverse && thrV < -0.02f) dir = 1;
    else if (thrV > 0.06f)        dir = -1;
  }
  roadShift += dir * (spd + 1);
  roadShift = ((roadShift % 36) + 36) % 36;

  bool braking = (brkV > 0.2f);
  bool parked  = (hbV > 0.2f);
  bool driving = (thrV > 0.06f);
  uint8_t pose;
  if (!linkLive)    pose = SC_SIT;
  else if (reverse) pose = SC_REVERSE;
  else if (braking) pose = SC_BRACE;
  else if (parked)  pose = SC_SIT;
  else if (driving) pose = SC_TROT;
  else              pose = SC_SIT;

  int lean = (int)(strV * 3.0f);
  int legFrame = (abs(roadShift) / 4) % 2;
  bool wag = ((animClock / 4) % 2) == 0;
  bool fast = (spd >= 2);
  bool pawOn = ((animClock / 3) % 2) == 0;

  // ---- Yellow band: gear | self IP (row 1) | WiFi strength + rate (row 2) ----
  display.setTextSize(2);
  display.setCursor(2, 0);
  if (lastPkt.gear == 7) {
    display.print("R");
    display.setTextSize(1);
    display.setCursor(14, 8);
    display.print("2");
  } else {
    display.print(gearStr(lastPkt.gear));
  }
  display.drawFastVLine(20, 0, YELLOW_H, SSD1306_WHITE);

  display.setTextSize(1);
  display.setCursor(24, 0);
  display.print(ipStr);                       // full self IP, always shown

  bool wconn = (WiFi.status() == WL_CONNECTED);
  int rssi = wconn ? WiFi.RSSI() : 0;
  int level = 0;
  if (wconn) {
    if (rssi >= -55) level = 4;
    else if (rssi >= -65) level = 3;
    else if (rssi >= -75) level = 2;
    else level = 1;
  }
  drawWifiBars(24, 15, level);
  display.setCursor(40, 8);
  if (wconn) { display.print(rssi); }
  else       { display.print("--"); }
  if (linkLive) {
    char pb[10]; snprintf(pb, sizeof(pb), "%upps", pktRate);
    display.setCursor(68, 8); display.print(pb);
  } else if ((millis() / 400) % 2) {
    display.setCursor(68, 8); display.print("FAILSAFE");
  }

  display.fillCircle(121, 8, pawOn ? 2 : 1, SSD1306_WHITE);
  display.drawPixel(118, 5, SSD1306_WHITE);
  display.drawPixel(121, 4, SSD1306_WHITE);
  display.drawPixel(124, 5, SSD1306_WHITE);

  // ---- Blue band, left: Scout on a scrolling road ----
  display.drawLine(2, 55, 42, 55, SSD1306_WHITE);
  for (int k = 0; k < 4; k++) {
    int x = 2 + (((k * 12 + roadShift) % 36) + 36) % 36;
    display.fillRect(x, 57, 4, 1, SSD1306_WHITE);
  }
  int sx = 2 + lean;
  if (sx < 0) sx = 0;
  if (sx > 8) sx = 8;
  drawScout(sx, pose, legFrame, wag, fast, !linkLive);

  // ---- Blue band, right: control gauges ----
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  const int bx = 66, bw = 60, bh = 6;
  display.setCursor(46, 17); display.print("STR"); drawBar(bx, 18, bw, bh, strV, true);
  display.setCursor(46, 27); display.print("THR"); drawBar(bx, 28, bw, bh, thrV, true);
  display.setCursor(46, 37); display.print("BRK"); drawBar(bx, 38, bw, bh, brkV, false);
  display.setCursor(46, 47); display.print("HB");  drawBar(bx, 48, bw, bh, hbV,  false);

  if (millis() < trimShownUntil) {
    char tb[16];
    snprintf(tb, sizeof(tb), "TRIM %+dus", steerTrimUs);
    display.setCursor(46, 56);
    display.print(tb);
  }

  display.display();
}

void sendTelemetry(uint32_t now, bool linkLive) {
  if (!haveSender) return;
  if (now - lastTeleMs < TELE_MS) return;
  lastTeleMs = now;
  TelemetryPacket tp;
  tp.magic = TELE_MAGIC;
  tp.version = TELE_VERSION;
  tp.seq = teleSeq++;
  tp.gear = lastPkt.gear;
  tp.drvState = (uint8_t)drvState;
  tp.linkLive = linkLive ? 1 : 0;
  tp.flags = lastPkt.flags;
  tp.steer = lastPkt.steer;
  tp.escOff = (int16_t)(currentEscUs - ESC_NEUTRAL_US);
  tp.brake = lastPkt.brake;
  tp.handbrake = lastPkt.handbrake;
  tp.trimUs = (int16_t)steerTrimUs;
  tp.pps = pktRate;
  tp.lost = lostCount;
  tp.rssi = (WiFi.status() == WL_CONNECTED) ? (int8_t)WiFi.RSSI() : 0;
  tp.resetReason = (uint8_t)esp_reset_reason();
  tp.freeHeapKb = (uint16_t)(esp_get_free_heap_size() / 1024);
  udp.beginPacket(senderIp, senderPort);
  udp.write((uint8_t*)&tp, sizeof(tp));
  udp.endPacket();
}

// ---- OTA rollback protection --------------------------------------------------
// With a dead USB port, a bad OTA image would brick the car. The bootloader has app
// rollback enabled (CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y in Arduino-ESP32 3.x), so:
//   1. A freshly flashed image boots in state PENDING_VERIFY.
//   2. We confirm it (esp_ota_mark_app_valid_cancel_rollback) only after WiFi is up,
//      the OTA service is listening, and the loop has run ROLLBACK_CONFIRM_MS.
//   3. If it resets before that (crash, watchdog, brownout) the bootloader boots the
//      previous image instead. If it hangs, or can't join WiFi, a timer on the
//      esp_timer task (which keeps running even if setup()/loop() is wedged) forces
//      the rollback at ROLLBACK_DEADLINE_MS.
// The previous image is the one that just flashed this one over WiFi, so it is known
// to reach WiFi + OTA: you can always flash again.

// The Arduino core auto-confirms new images at startup unless this returns true.
// C linkage: it overrides a weak C function in the core (esp32-hal-misc.c).
extern "C" bool verifyRollbackLater() { return true; }

bool otaPendingVerify = false;
esp_timer_handle_t rollbackTimer = nullptr;

static void rollbackDeadline(void*) {
  esp_ota_mark_app_invalid_rollback_and_reboot();   // does not return on success
}

void startRollbackGuard() {
  esp_ota_img_states_t st;
  const esp_partition_t* running = esp_ota_get_running_partition();
  if (esp_ota_get_state_partition(running, &st) != ESP_OK || st != ESP_OTA_IMG_PENDING_VERIFY) return;
  otaPendingVerify = true;
  esp_timer_create_args_t args = {};
  args.callback = &rollbackDeadline;
  args.name = "rollback";
  if (esp_timer_create(&args, &rollbackTimer) == ESP_OK) {
    esp_timer_start_once(rollbackTimer, (uint64_t)ROLLBACK_DEADLINE_MS * 1000ULL);
  }
}

void confirmImageIfHealthy(uint32_t now) {
  if (!otaPendingVerify) return;
  if (now < ROLLBACK_CONFIRM_MS || WiFi.status() != WL_CONNECTED) return;
  if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
    otaPendingVerify = false;
    if (rollbackTimer) {
      esp_timer_stop(rollbackTimer);
      esp_timer_delete(rollbackTimer);
      rollbackTimer = nullptr;
    }
  }
}

void setupOTA() {
  ArduinoOTA.setHostname(OTA_HOSTNAME);
#ifdef OTA_PASSWORD
  ArduinoOTA.setPassword(OTA_PASSWORD);
#endif
  ArduinoOTA.onStart([]() {
    esp_task_wdt_delete(NULL);   // flashing blocks for seconds — don't let the WDT reboot mid-update
    // Safety: hold neutral outputs during the update.
    esc.writeMicroseconds(ESC_NEUTRAL_US);
    steerServo.writeMicroseconds(STEER_CENTER_US + steerTrimUs);
    display.ssd1306_command(SSD1306_DISPLAYON);  // ensure OTA progress is visible even if OLED was toggled off
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println("OTA UPDATE");
    display.display();
  });
  ArduinoOTA.onProgress([](unsigned int prog, unsigned int total) {
    int pct = total ? (int)((prog * 100UL) / total) : 0;
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);  display.print("OTA UPDATE");
    display.setCursor(0, 14); display.print(pct); display.print("%");
    display.drawRect(0, 30, 128, 10, SSD1306_WHITE);
    display.fillRect(2, 32, (124 * pct) / 100, 6, SSD1306_WHITE);
    display.display();
  });
  ArduinoOTA.onError([](ota_error_t err) {
    display.setCursor(0, 50);
    display.print("OTA err ");
    display.print((int)err);
    display.display();
  });
  ArduinoOTA.begin();   // also starts mDNS as OTA_HOSTNAME.local

  // Advertise the control port + firmware version so the laptop tools can show
  // what the car is running (tools/find_car.py, tools/flash.py).
  MDNS.addService("rccar", "udp", UDP_PORT);
  MDNS.addServiceTxt("rccar", "udp", "fw", FW_VERSION);
  MDNS.addServiceTxt("rccar", "udp", "proto", "1");
}

// WiFi networks in priority order. Edit credentials in secrets.h.
struct WifiCred { const char* ssid; const char* pass; };
static const WifiCred wifiCreds[] = {
  { WIFI_SSID_1, WIFI_PASS_1 },   // primary
  { WIFI_SSID_2, WIFI_PASS_2 },   // fallback
};
static const int NUM_WIFI = sizeof(wifiCreds) / sizeof(wifiCreds[0]);

// Try each network once in priority order. Returns true on the first that connects.
// Blocking, but bounded to perNetworkMs per network so it always falls through to the
// fallback. Keeps the ESC at neutral while waiting.
bool connectWifi(uint32_t perNetworkMs = 8000) {
  for (int i = 0; i < NUM_WIFI; i++) {
    Serial.printf("WiFi: trying %s ", wifiCreds[i].ssid);
    WiFi.begin(wifiCreds[i].ssid, wifiCreds[i].pass);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < perNetworkMs) {
      esc.writeMicroseconds(ESC_NEUTRAL_US);
      delay(300);
      Serial.print(".");
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("WiFi: connected to %s, IP = %s\n",
                    wifiCreds[i].ssid, WiFi.localIP().toString().c_str());
      strncpy(ipStr, WiFi.localIP().toString().c_str(), sizeof(ipStr) - 1);
      ipStr[sizeof(ipStr) - 1] = '\0';
      WiFi.setTxPower(WIFI_POWER_19_5dBm);  // max TX power for range
      return true;
    }
  }
  return false;
}

void setup() {
  startRollbackGuard();             // first: covers everything below, even a hang
  Serial.begin(115200);
  Wire.begin(21, 22);
  Wire.setClock(400000);            // 400 kHz I2C: OLED flush ~4x faster, frees the control loop
  pinMode(BTN_PIN, INPUT_PULLUP);   // onboard button toggles the OLED
  display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  prefs.begin("carlink", false);
  steerTrimUs = prefs.getInt("steerTrim", 0);
  if (steerTrimUs >  TRIM_MAX_US) steerTrimUs =  TRIM_MAX_US;
  if (steerTrimUs < -TRIM_MAX_US) steerTrimUs = -TRIM_MAX_US;

  steerServo.setPeriodHertz(50);
  steerServo.attach(STEER_PIN, 1000, 2000);
  steerServo.writeMicroseconds(STEER_CENTER_US + steerTrimUs);

  esc.setPeriodHertz(50);
  esc.attach(ESC_PIN, 1000, 2000);
  esc.writeMicroseconds(ESC_NEUTRAL_US);

  display.setCursor(0, 4);
  display.println("Neutral active");
  display.setCursor(0, 24);
  display.println("Switch ESC ON to arm");
  display.setCursor(0, 44);
  display.print("fw " FW_VERSION " " OTA_HOSTNAME);
  if (otaPendingVerify) { display.setCursor(0, 54); display.print("new fw: verifying"); }
  display.display();

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);             // disable modem power-save: kills periodic UDP latency spikes
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  // Cycle the whole network list until something connects. Never give up: OTA is the
  // only way to flash (USB pins are dead), so the link must come back on its own.
  while (!connectWifi()) {
    Serial.println("WiFi: no known network reachable, retrying...");
  }
  wifiDownSinceMs = millis();

  setupOTA();

  udp.begin(UDP_PORT);
  memset(&lastPkt, 0, sizeof(lastPkt));
  lastPkt.gear = 0;
  rateWindowMs = millis();
  lastSlewMs = millis();

  // Arm the task watchdog now that boot (including the blocking WiFi connect) is done.
  // If loop() ever stops feeding it for WDT_TIMEOUT_S, the chip reboots itself.
#if ESP_IDF_VERSION_MAJOR >= 5
  esp_task_wdt_config_t wdtCfg = { .timeout_ms = WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true };
  esp_task_wdt_reconfigure(&wdtCfg);   // TWDT is pre-initialized by the core on IDF 5.x
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);   // subscribe this loop task
}

void loop() {
  ArduinoOTA.handle();          // service wireless uploads (no-op when idle)
  esp_task_wdt_reset();         // feed the watchdog; a hung loop won't reach here -> auto-reboot
  confirmImageIfHealthy(millis());   // new firmware: mark it good once proven (no-op after)

  int len = udp.parsePacket();
  while (len > 0) {
    int n = udp.read(rxBuf, sizeof(rxBuf));
    if (n == (int)sizeof(DrivePacket)) {
      DrivePacket pkt;
      memcpy(&pkt, rxBuf, sizeof(pkt));
      if (pkt.magic == MAGIC && pkt.version == VERSION) {
        if (pktCount > 0 && pkt.seq > lastSeq + 1) lostCount += (pkt.seq - lastSeq - 1);
        lastSeq = pkt.seq;
        lastPkt = pkt;
        lastPktMs = millis();
        pktCount++;
        senderIp = udp.remoteIP();
        senderPort = udp.remotePort();
        haveSender = true;
      }
    }
    len = udp.parsePacket();
  }

  uint32_t now = millis();
  if (now - rateWindowMs >= 1000) { pktRate = pktCount; pktCount = 0; rateWindowMs = now; }

  uint32_t linkGap = (lastPktMs != 0) ? (now - lastPktMs) : 0xFFFFFFFF;
  bool linkLive = (lastPktMs != 0) && (linkGap < FAILSAFE_MS);

  updateTrim(now, linkLive ? lastPkt.flags : 0, linkLive);

  if (trimDirty && (now - trimChangedMs >= TRIM_SAVE_MS)) {
    prefs.putInt("steerTrim", steerTrimUs);
    trimDirty = false;
  }

  int targetSteer, targetEsc;
  if (linkLive) {
    targetSteer = steerTargetUs(lastPkt.steer);
    targetEsc = escTargetUs(now, lastPkt.gear, lastPkt.drive, lastPkt.brake, lastPkt.handbrake);
  }
#if FAILSAFE_HOLD_LAST
  else if (lastPktMs != 0 && linkGap < FAILSAFE_MS + FAILSAFE_HOLD_MS) {
    // Brief dropout: hold the last steering angle and freeze the throttle where it is
    // (steerTargetUs is side-effect-free; freezing target to currentEscUs = no change).
    // Smooths RF flicker around the threshold instead of surging to neutral and back.
    targetSteer = steerTargetUs(lastPkt.steer);
    targetEsc = currentEscUs;
  }
#endif
  else {
    targetSteer = STEER_CENTER_US + steerTrimUs;
    targetEsc = ESC_NEUTRAL_US;
    rvsState = RVS_IDLE;
    drvState = DRV_BLOCKED;
  }

  float dt = (now - lastSlewMs) / 1000.0f;
  lastSlewMs = now;
  currentSteerUs = slewToward(currentSteerUs, targetSteer, STEER_SLEW, dt);
  currentEscUs = slewToward(currentEscUs, targetEsc, ESC_SLEW, dt);

  steerServo.writeMicroseconds(currentSteerUs);
  esc.writeMicroseconds(currentEscUs);

  sendTelemetry(now, linkLive);

  // Onboard button: tap to toggle the OLED. Off = no I2C traffic, so the loop runs unimpeded.
  bool btnNow = (digitalRead(BTN_PIN) == LOW);
  if (btnNow && !btnPrev && (now - lastBtnMs > 250)) {
    oledOn = !oledOn;
    lastBtnMs = now;
    display.ssd1306_command(oledOn ? SSD1306_DISPLAYON : SSD1306_DISPLAYOFF);
  }
  btnPrev = btnNow;

  // Non-blocking WiFi recovery. WiFi.begin() returns immediately and the association
  // completes on the background WiFi task, so the control loop NEVER stalls here. (The
  // old blocking connectWifi(5000)/WiFi.reconnect() froze the loop for up to 5s on a
  // marginal signal — the root cause of mid-drive glitching and the buffer-overflow loss
  // bursts seen in the logs.) While down, rotate through the network list every
  // WIFI_RETRY_MS so we can still fail over to the backup SSID without ever blocking.
  static bool wifiWasUp = true;
  bool wifiUp = (WiFi.status() == WL_CONNECTED);
  if (wifiUp) {
    wifiDownSinceMs = now;
    if (!wifiWasUp) {   // just (re)connected — refresh the shown IP (network may have changed)
      strncpy(ipStr, WiFi.localIP().toString().c_str(), sizeof(ipStr) - 1);
      ipStr[sizeof(ipStr) - 1] = '\0';
    }
  } else if (now - lastReconnectMs >= WIFI_RETRY_MS) {
    static int nextNet = 0;
    WiFi.begin(wifiCreds[nextNet].ssid, wifiCreds[nextNet].pass);  // non-blocking
    nextNet = (nextNet + 1) % NUM_WIFI;
    lastReconnectMs = now;
  }
  wifiWasUp = wifiUp;

  static uint32_t lastDraw = 0;
  // 5 Hz redraw: each full SSD1306 I2C flush blocks the loop ~20-30ms, so halving the
  // rate (was 100ms/10Hz) roughly halves control-loop stalls. OLED off (BOOT btn) = none.
  if (oledOn && now - lastDraw >= 200) { drawStatus(linkLive); lastDraw = now; }
}
