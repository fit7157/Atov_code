import cv2
import mediapipe as mp
import serial
import time
import threading
import queue
import requests


# ============================================================
# Arduino로 보내는 특수 신호
# ============================================================
FALL_SIGNAL = 998
STOP_SIGNAL = 999

SLOUCH_SIGNAL = "SLOUCH"
SWAY_SIGNAL = "SWAY"


# ============================================================
# IoTCOSS / oneM2M 설정
# ============================================================
# API_KEY에는 기존 Arduino secrets.h 또는 Android 앱에서 쓰던 API Key를 넣어야 한다.
API_HOST = "https://onem2m.iotcoss.ac.kr"
CSEBASE = "/Mobius"

ORIGIN = "SOrigin_20011069_t1"
API_KEY = "tF1wuKhyULlNGPXXYhVY7sAB4mNyVwRb"
CREATOR = "sju20011069"
LECTURE = "LCT_20260002"

AE_RN = "R4_TUTO_" + ORIGIN
AE_PATH = f"{CSEBASE}/{AE_RN}"

FALL_CNT = "FALL"
POSTURE_CNT = "POSTURE"
SWAY_CNT = "SWAY"

FALL_PATH = f"{AE_PATH}/{FALL_CNT}"
POSTURE_PATH = f"{AE_PATH}/{POSTURE_CNT}"
SWAY_PATH = f"{AE_PATH}/{SWAY_CNT}"

HTTP_TIMEOUT = 1.5
upload_queue = queue.Queue(maxsize=50)


# ============================================================
# ArUco 마커 ID와 구역 이름 매핑
# ============================================================
ZONE_MAP = {
    1: "A",
    2: "B",
    3: "C",
    4: "D"
}

last_seen_zone = "UNKNOWN"
last_seen_marker_id = -1


# ============================================================
# 시리얼 전송 제어 변수
# ============================================================
fall_signal_sent = False

CONTROL_INTERVAL = 0.04
ERROR_CHANGE_THRESHOLD = 4

last_send_time = 0
last_sent_command = None


# ============================================================
# 구부정/어깨 기울어짐 카운트 설정
# ============================================================
SHOULDER_DIFF_THRESHOLD = 0.08
SLOUCH_CONFIRM_FRAMES = 5

slouch_count = 0
slouch_frame_count = 0
slouch_active = False
last_slouch_direction = "NONE"


# ============================================================
# 중심점 좌우 흔들림 / 휘청임 카운트 설정
# ============================================================
SWAY_PIXEL_THRESHOLD = 45
SWAY_CONFIRM_FRAMES = 3
SWAY_COOLDOWN_SECONDS = 1.0
SWAY_HISTORY_SIZE = 8

sway_count = 0
sway_frame_count = 0
sway_active = False
last_sway_direction = "NONE"
last_sway_send_time = 0.0
sway_center_history = []


# ============================================================
# IoTCOSS 업로드 함수
# ============================================================
def make_headers(request_label, resource_type=None):
    headers = {
        "Accept": "application/json",
        "X-M2M-RI": f"{request_label}_{int(time.time() * 1000)}",
        "X-M2M-RVI": "2a",
        "X-M2M-Origin": ORIGIN,
        "X-API-KEY": API_KEY,
        "X-AUTH-CUSTOM-LECTURE": LECTURE,
        "X-AUTH-CUSTOM-CREATOR": CREATOR,
    }

    if resource_type == "AE":
        headers["Content-Type"] = "application/json;ty=2"
    elif resource_type == "CNT":
        headers["Content-Type"] = "application/json;ty=3"
    elif resource_type == "CIN":
        headers["Content-Type"] = "application/json;ty=4"

    return headers


def get_resource_status(path):
    url = API_HOST + path

    try:
        response = requests.get(
            url,
            headers=make_headers("pi_get"),
            timeout=HTTP_TIMEOUT
        )
        return response.status_code
    except Exception as e:
        print(f"[IoTCOSS] GET 실패: {path} / {e}")
        return -1


def post_resource(parent_path, resource_type, resource_name="", content=""):
    url = API_HOST + parent_path

    if resource_type == "AE":
        body = {
            "m2m:ae": {
                "rn": resource_name,
                "api": "NRobot",
                "rr": True,
                "srv": ["2a"]
            }
        }
    elif resource_type == "CNT":
        body = {
            "m2m:cnt": {
                "rn": resource_name,
                "mbs": 16384
            }
        }
    elif resource_type == "CIN":
        body = {
            "m2m:cin": {
                "con": content
            }
        }
    else:
        return -1

    try:
        response = requests.post(
            url,
            headers=make_headers("pi_post", resource_type),
            json=body,
            timeout=HTTP_TIMEOUT
        )
        return response.status_code
    except Exception as e:
        print(f"[IoTCOSS] POST 실패: {parent_path} / {e}")
        return -1


def is_success_status(status):
    return status in (200, 201, 204, 409)


def ensure_container(container_path, parent_path, container_name):
    status = get_resource_status(container_path)

    if status in (200, 201):
        return True

    if status == 404:
        status = post_resource(
            parent_path=parent_path,
            resource_type="CNT",
            resource_name=container_name
        )

        print(f"[IoTCOSS] CNT 생성 {container_name} -> HTTP {status}")

        return is_success_status(status)

    print(f"[IoTCOSS] CNT 확인 실패 {container_name} -> HTTP {status}")
    return False


def setup_iotcoss_resources():
    ae_status = get_resource_status(AE_PATH)

    if ae_status not in (200, 201):
        ae_status = post_resource(
            parent_path=CSEBASE,
            resource_type="AE",
            resource_name=AE_RN
        )

        print(f"[IoTCOSS] AE 생성 {AE_RN} -> HTTP {ae_status}")

        if not is_success_status(ae_status):
            return False

    fall_ok = ensure_container(FALL_PATH, AE_PATH, FALL_CNT)
    posture_ok = ensure_container(POSTURE_PATH, AE_PATH, POSTURE_CNT)
    sway_ok = ensure_container(SWAY_PATH, AE_PATH, SWAY_CNT)

    return fall_ok and posture_ok and sway_ok


def upload_cin(container_name, content):
    if container_name == FALL_CNT:
        path = FALL_PATH
    elif container_name == POSTURE_CNT:
        path = POSTURE_PATH
    elif container_name == SWAY_CNT:
        path = SWAY_PATH
    else:
        print(f"[IoTCOSS] 알 수 없는 container: {container_name}")
        return False

    status = post_resource(
        parent_path=path,
        resource_type="CIN",
        content=content
    )

    if is_success_status(status):
        print(f"[IoTCOSS] 업로드 성공: {container_name} / {content}")
        return True

    print(f"[IoTCOSS] 업로드 실패: {container_name} / HTTP {status} / {content}")
    return False


def enqueue_upload(container_name, content):
    try:
        upload_queue.put_nowait((container_name, content))
        print(f"[QUEUE] 업로드 예약: {container_name} / {content}")
    except queue.Full:
        try:
            dropped = upload_queue.get_nowait()
            upload_queue.task_done()
            print(f"[QUEUE] 큐 가득 참. 오래된 이벤트 삭제: {dropped}")
        except Exception:
            pass

        try:
            upload_queue.put_nowait((container_name, content))
            print(f"[QUEUE] 업로드 예약: {container_name} / {content}")
        except Exception as e:
            print(f"[QUEUE] 업로드 예약 실패: {e}")


def upload_worker():
    resources_ready = False
    last_resource_check_time = 0.0

    while True:
        container_name, content = upload_queue.get()

        try:
            now = time.monotonic()

            if (not resources_ready) or (now - last_resource_check_time > 60.0):
                resources_ready = setup_iotcoss_resources()
                last_resource_check_time = now

            if resources_ready:
                upload_cin(container_name, content)
            else:
                print("[IoTCOSS] 리소스 준비 실패. 이벤트 업로드 건너뜀.")

        except Exception as e:
            print(f"[IoTCOSS] 업로드 스레드 오류: {e}")

        finally:
            upload_queue.task_done()


threading.Thread(
    target=upload_worker,
    daemon=True
).start()


# ============================================================
# Arduino 시리얼 연결
# ============================================================
try:
    # Arduino는 이제 모터 제어만 담당한다.
    ser = serial.Serial('/dev/ttyACM0', 9600, timeout=1)

    time.sleep(2)
    ser.reset_input_buffer()

    print("모터 제어 Arduino 연결 성공!")

except Exception as e:
    print(f"아두이노 연결 실패: {e}")
    exit()


# ============================================================
# Arduino로 모터 명령을 전송하는 함수
# ============================================================
def is_int_like(value):
    try:
        int(value)
        return True
    except Exception:
        return False


def send_motor_command(command, force=False):
    global last_send_time, last_sent_command

    now = time.monotonic()

    if force:
        ser.write(f"{command}\n".encode())
        last_send_time = now
        last_sent_command = command
        return

    if command == STOP_SIGNAL and command != last_sent_command:
        ser.write(f"{command}\n".encode())
        last_send_time = now
        last_sent_command = command
        return

    should_send = False

    if now - last_send_time >= CONTROL_INTERVAL:
        should_send = True

    if is_int_like(command) and is_int_like(last_sent_command):
        if abs(int(command) - int(last_sent_command)) >= ERROR_CHANGE_THRESHOLD:
            should_send = True

    if should_send:
        ser.write(f"{command}\n".encode())
        last_send_time = now
        last_sent_command = command


# ============================================================
# MediaPipe Pose 초기화
# ============================================================
mp_pose = mp.solutions.pose

pose = mp_pose.Pose(
    model_complexity=1,
    min_detection_confidence=0.4,
    min_tracking_confidence=0.5
)

mp_draw = mp.solutions.drawing_utils


# ============================================================
# ArUco 마커 인식 초기화
# ============================================================
aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

try:
    aruco_params = cv2.aruco.DetectorParameters()
    aruco_detector = cv2.aruco.ArucoDetector(aruco_dict, aruco_params)
    USE_NEW_ARUCO = True
except Exception:
    aruco_params = cv2.aruco.DetectorParameters_create()
    aruco_detector = None
    USE_NEW_ARUCO = False


def detect_zone_from_aruco(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    if USE_NEW_ARUCO:
        corners, ids, rejected = aruco_detector.detectMarkers(gray)
    else:
        corners, ids, rejected = cv2.aruco.detectMarkers(
            gray,
            aruco_dict,
            parameters=aruco_params
        )

    detected_zone = None
    detected_marker_id = None

    if ids is not None:
        cv2.aruco.drawDetectedMarkers(frame, corners, ids)

        best_area = 0

        for i, marker_id in enumerate(ids.flatten()):
            marker_id = int(marker_id)

            if marker_id not in ZONE_MAP:
                continue

            c = corners[i].reshape((4, 2))
            area = cv2.contourArea(c)

            if area > best_area:
                best_area = area
                detected_marker_id = marker_id
                detected_zone = ZONE_MAP[marker_id]

    return detected_zone, detected_marker_id


# ============================================================
# 구부정/어깨 기울어짐 판단 함수
# ============================================================
def update_slouch_count(l_shoulder_y, r_shoulder_y, current_zone):
    global slouch_count, slouch_frame_count, slouch_active, last_slouch_direction

    shoulder_y_diff = abs(l_shoulder_y - r_shoulder_y)

    if l_shoulder_y > r_shoulder_y:
        direction = "LEFT_LOW"
    else:
        direction = "RIGHT_LOW"

    if shoulder_y_diff >= SHOULDER_DIFF_THRESHOLD:
        slouch_frame_count += 1

        if not slouch_active and slouch_frame_count >= SLOUCH_CONFIRM_FRAMES:
            slouch_active = True
            slouch_count += 1
            last_slouch_direction = direction

            posture_content = (
                f"SLOUCH_COUNT={slouch_count},"
                f"ZONE={current_zone},"
                f"DIRECTION={direction}"
            )

            enqueue_upload(POSTURE_CNT, posture_content)

            print(
                f"구부정/어깨 기울어짐 이벤트: {posture_content} "
                f"diff={shoulder_y_diff:.3f}"
            )

    else:
        slouch_frame_count = 0
        slouch_active = False
        last_slouch_direction = "NONE"

    return shoulder_y_diff, slouch_count, last_slouch_direction


# ============================================================
# 중심점 좌우 흔들림 / 휘청임 판단 함수
# ============================================================
def update_sway_count(cx, current_zone):
    global sway_count, sway_frame_count, sway_active
    global last_sway_direction, last_sway_send_time, sway_center_history

    sway_center_history.append(cx)

    if len(sway_center_history) > SWAY_HISTORY_SIZE:
        sway_center_history.pop(0)

    if len(sway_center_history) < SWAY_HISTORY_SIZE:
        return 0, sway_count, last_sway_direction

    old_cx = sway_center_history[0]
    delta_x = cx - old_cx

    if abs(delta_x) >= SWAY_PIXEL_THRESHOLD:
        sway_frame_count += 1

        if delta_x > 0:
            direction = "RIGHT"
        else:
            direction = "LEFT"

        now = time.monotonic()

        if (
            not sway_active
            and sway_frame_count >= SWAY_CONFIRM_FRAMES
            and now - last_sway_send_time >= SWAY_COOLDOWN_SECONDS
        ):
            sway_active = True
            sway_count += 1
            last_sway_direction = direction
            last_sway_send_time = now

            sway_content = (
                f"SWAY_COUNT={sway_count},"
                f"ZONE={current_zone},"
                f"DIRECTION={direction}"
            )

            enqueue_upload(SWAY_CNT, sway_content)

            print(
                f"중심점 좌우 흔들림 이벤트: {sway_content} "
                f"delta_x={delta_x}"
            )

    else:
        sway_frame_count = 0
        sway_active = False
        last_sway_direction = "NONE"

    return delta_x, sway_count, last_sway_direction


# ============================================================
# 카메라 설정
# ============================================================
cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

if not cap.isOpened():
    print("카메라를 열 수 없습니다.")
    ser.close()
    exit()


# ============================================================
# 제어 설정
# ============================================================
STOP_THRESHOLD = 0.4
tracking_mode = True

print("Raspberry Pi 직접 IoTCOSS 업로드 + Arduino 모터 전용 시스템 시작... 'q'를 누르면 종료됩니다.")


try:
    while cap.isOpened():
        ret, frame = cap.read()

        if not ret:
            break

        frame = cv2.resize(frame, (640, 480))

        # ====================================================
        # 1. ArUco 마커로 현재 구역 인식
        # ====================================================
        detected_zone, detected_marker_id = detect_zone_from_aruco(frame)

        if detected_zone is not None:
            last_seen_zone = detected_zone
            last_seen_marker_id = detected_marker_id

        cv2.putText(
            frame,
            f"ZONE: {last_seen_zone}  ID: {last_seen_marker_id}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 255),
            2
        )

        # ====================================================
        # 2. MediaPipe 사람 추적
        # ====================================================
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = pose.process(img_rgb)

        status_text = "Searching..."
        color = (255, 255, 255)
        shoulder_info_text = f"SLOUCH CNT: {slouch_count}"
        sway_info_text = f"SWAY CNT: {sway_count}"
        upload_info_text = f"UPLOAD QUEUE: {upload_queue.qsize()}"

        if results.pose_landmarks:
            fall_signal_sent = False

            mp_draw.draw_landmarks(
                frame,
                results.pose_landmarks,
                mp_pose.POSE_CONNECTIONS
            )

            lm = results.pose_landmarks.landmark

            l_wrist_y = lm[15].y
            r_wrist_y = lm[16].y

            l_shoulder_y = lm[11].y
            r_shoulder_y = lm[12].y

            cx = int((lm[11].x + lm[12].x) * 640 / 2)
            cy = int((lm[11].y + lm[12].y) * 480 / 2)

            shoulder_diff, current_slouch_count, current_slouch_direction = update_slouch_count(
                l_shoulder_y,
                r_shoulder_y,
                last_seen_zone
            )

            shoulder_info_text = (
                f"SH_DIFF: {shoulder_diff:.3f}  "
                f"SLOUCH CNT: {current_slouch_count}  "
                f"DIR: {current_slouch_direction}"
            )

            sway_delta, current_sway_count, current_sway_direction = update_sway_count(
                cx,
                last_seen_zone
            )

            sway_info_text = (
                f"SWAY_DX: {sway_delta}  "
                f"SWAY CNT: {current_sway_count}  "
                f"DIR: {current_sway_direction}"
            )

            error = cx - 320
            shoulder_width = abs(lm[11].x - lm[12].x)

            gesture_detected = False

            if l_wrist_y < l_shoulder_y and r_wrist_y < r_shoulder_y:
                tracking_mode = False
                gesture_detected = True

                status_text = "GESTURE: STOP MODE"
                color = (0, 0, 255)

            elif l_wrist_y < l_shoulder_y or r_wrist_y < r_shoulder_y:
                tracking_mode = True
                gesture_detected = True

                status_text = "GESTURE: Resume Tracking"
                color = (0, 255, 0)

            if tracking_mode:
                if shoulder_width > STOP_THRESHOLD:
                    send_motor_command(STOP_SIGNAL)

                    if not gesture_detected:
                        status_text = "TOO CLOSE - STOPPED"
                        color = (0, 165, 255)

                else:
                    send_motor_command(error)

                    if not gesture_detected:
                        status_text = f"Tracking - Error: {error}"
                        color = (0, 255, 0)

            else:
                send_motor_command(STOP_SIGNAL)

                if not gesture_detected:
                    status_text = "STOPPED BY GESTURE"
                    color = (0, 0, 255)

            cv2.circle(frame, (cx, cy), 10, color, -1)

        else:
            sway_center_history.clear()
            sway_frame_count = 0
            sway_active = False
            last_sway_direction = "NONE"

            if not fall_signal_sent:
                # Arduino에는 정지 신호만 전송
                send_motor_command(FALL_SIGNAL, force=True)

                # IoTCOSS에는 Pi가 직접 업로드
                fall_content = f"FALL_DETECTED,ZONE={last_seen_zone}"
                enqueue_upload(FALL_CNT, fall_content)

                fall_signal_sent = True

                print(f"낙상 이벤트: {fall_content}")

            status_text = f"FALL DETECTED - ZONE {last_seen_zone}"
            color = (0, 0, 255)

        cv2.putText(
            frame,
            status_text,
            (10, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            color,
            2
        )

        cv2.putText(
            frame,
            shoulder_info_text,
            (10, 105),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 0),
            2
        )

        cv2.putText(
            frame,
            sway_info_text,
            (10, 135),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 255),
            2
        )

        cv2.putText(
            frame,
            upload_info_text,
            (10, 165),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (180, 255, 180),
            2
        )

        cv2.imshow("ATOV Pi Upload + Motor Tracking", frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break


finally:
    if 'ser' in locals() and ser.is_open:
        try:
            send_motor_command(STOP_SIGNAL, force=True)
        except Exception:
            pass

        ser.close()

    cap.release()
    pose.close()
    cv2.destroyAllWindows()
