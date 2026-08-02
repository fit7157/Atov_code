/*
  ATOV 모터 제어 전용 Arduino 코드

  역할:
  - Raspberry Pi에서 받은 tracking error 값으로 모터 제어
  - 999 수신 시 정지
  - 998 수신 시 낙상 정지 + 버저 울림

  서버 업로드는 Raspberry Pi가 직접 담당한다.
  따라서 이 Arduino 코드에는 WiFi / HTTPS / IoTCOSS 코드가 없다.
*/

// ============================================================
// 1. 모터 드라이버 핀 설정
// ============================================================
const int ENA = 10;
const int IN1 = 9;
const int IN2 = 8;
const int IN3 = 7;
const int IN4 = 6;
const int ENB = 5;

// ============================================================
// 1-1. 버저 핀 설정
// ============================================================
// 3핀 수동 부저 모듈 기준:
// S → Arduino D4
// V → Arduino 5V
// G → Arduino GND
const int BUZZER_PIN = 4;

// 낙상 발생 시 버저가 울리는 시간
const unsigned long FALL_BUZZER_DURATION = 5000UL;  // 5초

// 삐-삐-삐 간격
const unsigned long BUZZER_INTERVAL = 250UL;

// 수동 부저 주파수
const int BUZZER_FREQUENCY = 2000;

bool fallBuzzerActive = false;
bool buzzerState = false;
unsigned long fallBuzzerStartTime = 0;
unsigned long lastBuzzerToggleTime = 0;

// ============================================================
// 2. 사람 추적 주행 설정
// ============================================================
int baseSpeed = 210;  //150
float turnSensitivity = 0.1;  //0.05

// 좌우 모터의 차이를 보정
const int LEFT_MOTOR_TRIM = 0;
const int RIGHT_MOTOR_TRIM = 0;

const int ERROR_DEADZONE = 25;
const int MIN_TURN_PWM = 30;
const float TURN_EXTRA_GAIN = 0.15;
const int MAX_TURN_PWM = 70;

const int FALL_SIGNAL = 998;
const int STOP_SIGNAL = 999;

// 일정 시간 동안 새 tracking error가 들어오지 않으면 정지
const unsigned long TRACKING_COMMAND_TIMEOUT = 450UL;
unsigned long lastTrackingCommandTime = 0;


// ============================================================
// setup
// ============================================================
void setup() {
  Serial.begin(9600);

  pinMode(ENA, OUTPUT);
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  pinMode(ENB, OUTPUT);

  pinMode(BUZZER_PIN, OUTPUT);
  noTone(BUZZER_PIN);

  stopRobot();
}


// ============================================================
// loop
// ============================================================
void loop() {
  receivePiCommand();

  if (lastTrackingCommandTime > 0 &&
      millis() - lastTrackingCommandTime > TRACKING_COMMAND_TIMEOUT) {
    stopRobot();
    lastTrackingCommandTime = 0;
  }

  // 낙상 버저 상태 업데이트
  updateFallBuzzer();
}


// ============================================================
// Raspberry Pi 시리얼 데이터 처리
// ============================================================
void receivePiCommand() {
  static String inputBuffer = "";

  String latestTrackingErrorCommand = "";

  while (Serial.available() > 0) {
    char received = (char)Serial.read();

    if (received == '\n') {
      inputBuffer.trim();

      if (inputBuffer.length() > 0) {
        if (isTrackingErrorCommand(inputBuffer)) {
          latestTrackingErrorCommand = inputBuffer;
        } else {
          processPiCommand(inputBuffer);
        }
      }

      inputBuffer = "";
    }

    else if (received != '\r') {
      if (inputBuffer.length() < 80) {
        inputBuffer += received;
      } else {
        inputBuffer = "";
      }
    }
  }

  if (latestTrackingErrorCommand.length() > 0) {
    processPiCommand(latestTrackingErrorCommand);
  }
}


// ============================================================
// Raspberry Pi 명령 해석
// ============================================================
void processPiCommand(String command) {
  command.trim();

  // 낙상 정지
  if (command == String(FALL_SIGNAL)) {
    stopRobot();
    startFallBuzzer();
    return;
  }

  // 혹시 Raspberry Pi가 "998,A" 형식으로 보내도 정지 + 버저 처리
  int commaIndex = command.indexOf(',');

  if (commaIndex != -1) {
    String signal = command.substring(0, commaIndex);
    signal.trim();

    if (signal == String(FALL_SIGNAL)) {
      stopRobot();
      startFallBuzzer();
      return;
    }
  }

  // 일반 정지
  if (command == String(STOP_SIGNAL)) {
    stopRobot();
    return;
  }

  // 일반 error 값이 아니면 무시
  if (!isIntegerString(command)) {
    return;
  }

  int error = command.toInt();

  lastTrackingCommandTime = millis();

  moveRobot(error);
}


// ============================================================
// 문자열 정수 확인
// ============================================================
bool isIntegerString(const String &text) {
  if (text.length() == 0) {
    return false;
  }

  int startIndex = 0;

  if (text[0] == '-' || text[0] == '+') {
    startIndex = 1;
  }

  if (startIndex >= text.length()) {
    return false;
  }

  for (int i = startIndex; i < text.length(); i++) {
    if (text[i] < '0' || text[i] > '9') {
      return false;
    }
  }

  return true;
}


bool isTrackingErrorCommand(const String &text) {
  if (!isIntegerString(text)) {
    return false;
  }

  int value = text.toInt();

  if (value == FALL_SIGNAL || value == STOP_SIGNAL) {
    return false;
  }

  return true;
}


// ============================================================
// 모터 제어
// ============================================================
void moveRobot(int error) {
  int turn = 0;

  if (abs(error) > ERROR_DEADZONE) {
    int errorOutsideDeadzone = abs(error) - ERROR_DEADZONE;

    int turnMagnitude =
      MIN_TURN_PWM +
      (int)(errorOutsideDeadzone * TURN_EXTRA_GAIN);

    turnMagnitude = constrain(turnMagnitude, MIN_TURN_PWM, MAX_TURN_PWM);

    if (error > 0) {
      // 사람이 화면 오른쪽에 있음
      turn = turnMagnitude;
    } else if (error < 0) {
      // 사람이 화면 왼쪽에 있음
      turn = -turnMagnitude;
    } else {
      // 사람이 정확히 중앙에 있음
      turn = 0;
    }
  }

  int leftSpeed = baseSpeed + turn + LEFT_MOTOR_TRIM;
  int rightSpeed = baseSpeed - turn + RIGHT_MOTOR_TRIM;

  leftSpeed = constrain(leftSpeed, 0, 255);
  rightSpeed = constrain(rightSpeed, 0, 255);

  digitalWrite(IN1, LOW);
  digitalWrite(IN2, HIGH);
  analogWrite(ENA, leftSpeed);

  digitalWrite(IN3, HIGH);
  digitalWrite(IN4, LOW);
  analogWrite(ENB, rightSpeed);
}


// ============================================================
// 로봇 정지
// ============================================================
void stopRobot() {
  digitalWrite(IN1, LOW);
  digitalWrite(IN2, LOW);

  digitalWrite(IN3, LOW);
  digitalWrite(IN4, LOW);

  analogWrite(ENA, 0);
  analogWrite(ENB, 0);
}


// ============================================================
// 낙상 버저 시작
// ============================================================
void startFallBuzzer() {
  fallBuzzerActive = true;
  buzzerState = true;

  fallBuzzerStartTime = millis();
  lastBuzzerToggleTime = millis();

  // 낙상 발생 직후 바로 소리 시작
  tone(BUZZER_PIN, BUZZER_FREQUENCY);
}


// ============================================================
// 낙상 버저 업데이트
// ============================================================
void updateFallBuzzer() {
  if (!fallBuzzerActive) {
    return;
  }

  unsigned long now = millis();

  // 5초가 지나면 버저 정지
  if (now - fallBuzzerStartTime >= FALL_BUZZER_DURATION) {
    fallBuzzerActive = false;
    buzzerState = false;
    noTone(BUZZER_PIN);
    return;
  }

  // 0.25초마다 삐-삐- 반복
  if (now - lastBuzzerToggleTime >= BUZZER_INTERVAL) {
    lastBuzzerToggleTime = now;

    buzzerState = !buzzerState;

    if (buzzerState) {
      tone(BUZZER_PIN, BUZZER_FREQUENCY);
    } else {
      noTone(BUZZER_PIN);
    }
  }
}
