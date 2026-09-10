from temp_py_package import continuous_read, find_sensor_port
import time

def main():
    port = find_sensor_port()
    if port is None:
        print("找不到任何序列埠，請確認感測器已連接")
        return
    print(f"使用序列埠: {port}")
    while True:
        temp = continuous_read(port)
        if temp is not None:
            print("{:.1f}度C".format(temp))
        else:
            print("讀取溫度失敗")
        time.sleep(1)

if __name__ == '__main__':
    main()