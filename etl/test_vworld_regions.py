import os

import requests
from dotenv import load_dotenv


load_dotenv()

VWORLD_API_URL = "https://api.vworld.kr/req/data"
VWORLD_API_KEY = os.getenv("VWORLD_API_KEY")
VWORLD_API_DOMAIN = os.getenv("VWORLD_API_DOMAIN")


BASE_PARAMS = {
    "key": VWORLD_API_KEY,
    "domain": VWORLD_API_DOMAIN,
    "service": "data",
    "version": "2.0",
    "request": "getfeature",
    "format": "json",
    "size": 1000,
    "page": 1,
    "geometry": "false",
    "attribute": "true",
    "crs": "EPSG:3857",
    "geomfilter": (
        "BOX("
        "13663271.680031825,"
        "3894007.9689600193,"
        "14817776.555251127,"
        "4688953.0631258525"
        ")"
    ),
}


def request_vworld(params):
    response = requests.get(
        VWORLD_API_URL,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    status = data.get("response", {}).get("status")

    if status != "OK":
        raise RuntimeError(
            f"VWORLD API error: {data}"
        )

    return (
        data["response"]
        ["result"]
        ["featureCollection"]
        ["features"]
    )


def get_sidos():
    params = {
        **BASE_PARAMS,
        "data": "LT_C_ADSIDO_INFO",
    }

    return request_vworld(params)


def get_sigungu(sido_code):
    params = {
        **BASE_PARAMS,
        "data": "LT_C_ADSIGG_INFO",
        "attrfilter": f"sig_cd:like:{sido_code}",
    }

    return request_vworld(params)


if __name__ == "__main__":
    print("=== 시도 조회 ===")

    sidos = get_sidos()

    for feature in sidos:
        props = feature["properties"]

        print(
            props.get("ctprvn_cd"),
            props.get("ctp_kor_nm"),
        )

    print()
    print("=== 서울특별시 시군구 조회 ===")

    sigungu_list = get_sigungu("11")

    for feature in sigungu_list:
        props = feature["properties"]

        print(
            props.get("sig_cd"),
            props.get("sig_kor_nm"),
        )