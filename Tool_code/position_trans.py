def sanitize_pos_to_key(pos_str):
    """
    支持输入字符串 '120.5, -60.0, 0' 
    或列表/元组 [120.5, -60.0, 0]
    """
    # 如果输入是列表或元组，先转成字符串
    if isinstance(pos_str, (list, tuple)):
        pos_str = ",".join(map(str, pos_str))
    
    clean_key = pos_str.replace(" ", "")
    clean_key = clean_key.replace(".", "p")
    clean_key = clean_key.replace("-", "n")
    clean_key = clean_key.replace(",", "_")
    return "P_" + clean_key
