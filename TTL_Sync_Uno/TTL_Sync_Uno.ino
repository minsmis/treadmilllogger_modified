/*
  TTL sync input bridge for Arduino Uno R3.

  Wiring for a 0-5V TTL source:
    TTL signal -> D2 (interrupt-capable input)
    TTL ground -> Uno GND
    Add an external 10k pulldown from D2 to GND so a disconnected input reads LOW.

  If the TTL source and Uno must be electrically isolated, use an optocoupler input
  stage instead of directly sharing grounds. Do not connect this 5V signal to the
  Teensy 4.0 GPIO.

  Serial protocol at 115200 baud:
    SYNC,STATE,HIGH|LOW,micros
    SYNC,EDGE,HIGH|LOW,micros
  The PC sends SYNC? after opening the port to query the current TTL state.
*/
#include <Arduino.h>
#include <string.h>

const uint8_t TTL_PIN = 2;
const uint8_t QUEUE_SIZE = 16;

struct EdgeEvent {
  uint32_t timestampUs;
  uint8_t high;
};

volatile EdgeEvent edgeQueue[QUEUE_SIZE];
volatile uint8_t queueHead = 0;
volatile uint8_t queueTail = 0;
volatile uint8_t ttlState = LOW;
volatile bool queueOverflow = false;

char commandBuffer[12];
uint8_t commandLength = 0;

void ttlChanged() {
  const uint8_t newState = digitalRead(TTL_PIN) == HIGH ? HIGH : LOW;
  if (newState == ttlState) return;

  const uint8_t nextHead = (queueHead + 1) % QUEUE_SIZE;
  if (nextHead == queueTail) {
    queueOverflow = true;
    ttlState = newState;
    return;
  }

  edgeQueue[queueHead].timestampUs = micros();
  edgeQueue[queueHead].high = newState;
  queueHead = nextHead;
  ttlState = newState;
}

void printState(const char *kind, uint8_t state, uint32_t timestampUs) {
  Serial.print("SYNC,");
  Serial.print(kind);
  Serial.print(',');
  Serial.print(state == HIGH ? "HIGH" : "LOW");
  Serial.print(',');
  Serial.println(timestampUs);
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();
    if (c == '\r' || c == '\n') {
      commandBuffer[commandLength] = '\0';
      if (strcmp(commandBuffer, "SYNC?") == 0) {
        noInterrupts();
        const uint8_t state = ttlState;
        interrupts();
        printState("STATE", state, micros());
      }
      commandLength = 0;
    } else if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = c;
    } else {
      commandLength = 0;
    }
  }
}

void drainEdgeQueue() {
  while (true) {
    EdgeEvent event;
    bool available = false;

    noInterrupts();
    if (queueTail != queueHead) {
      event.timestampUs = edgeQueue[queueTail].timestampUs;
      event.high = edgeQueue[queueTail].high;
      queueTail = (queueTail + 1) % QUEUE_SIZE;
      available = true;
    }
    interrupts();

    if (!available) break;
    printState("EDGE", event.high, event.timestampUs);
  }

  noInterrupts();
  const bool overflow = queueOverflow;
  queueOverflow = false;
  interrupts();
  if (overflow) {
    Serial.print("SYNC,ERROR,QUEUE_OVERFLOW,");
    Serial.println(micros());
  }
}

void setup() {
  pinMode(TTL_PIN, INPUT);
  Serial.begin(115200);

  ttlState = digitalRead(TTL_PIN) == HIGH ? HIGH : LOW;
  attachInterrupt(digitalPinToInterrupt(TTL_PIN), ttlChanged, CHANGE);
  printState("STATE", ttlState, micros());
}

void loop() {
  readSerialCommands();
  drainEdgeQueue();
}
