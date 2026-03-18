import datetime
def write_position_to_txt(txt_path, pos_str, point_index):
    """
    将位置信息写入TXT文件
    :param txt_path: TXT文件路径
    :param pos_str: 位置字符串（如 "-12716,-3260,0"）
    :param point_index: 第几个扫描点（便于定位）
    """
    # 获取当前时间戳（精确到毫秒）
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    # 拼接写入内容：时间戳 | 点序号 | 坐标
    content = f"{current_time} | 扫描点 {point_index} | 坐标: {pos_str}\n"
    # 以追加模式打开文件（a+），不存在则创建
    with open(txt_path, "a+", encoding="utf-8") as f:
        f.write(content)