# YouTube(유튜브)

```
uv run unshackle dl --list -vl all -al orig -sl all -q 1080,2160 -v h.264,vp9 -r SDR YT SCguuq8upmH1TmPhSI3nqYqg
```

## Information(정보)

- Authorization: Device Flow
- Security: UHD@L1, FHD@L3/SL2000
- Working Client Agent: Android VR
- Support Codec
  - Video: H264, VP9, AV1
  - Audio: AAC, OPUS, EAC3, AC3
  - Range: SDR, HDR10

## Tips

- Configure user settings within the `envied.yaml` file.
  사용자 설정은 `envied.yaml`에서 다음과 같이 사용하세요.

  ```
  services:
    YT:
      ## 사용자 환경설정
      ## User configuration
      # 해당 설정값이 주석처리 되어 있는 경우에는 설정값들이 자동으로 선택됩니다.
      # If these settings are commented out, values will be selected automatically.
      preferences:
        # 서비스 내에서 표시되는 메타데이터 언어를 선택합니다.
        # Selects the metadata language displayed within the service.
        # 언어 설정은 YouTube에서 지원하는 언어 코드(예: "ko", "en")만 사용 가능합니다.
        # Language settings are only available for language codes supported by YouTube (e.g., "ko", "en").
        # 값이 설정되지 않은 경우에는 기본으로 영어("en")를 사용합니다.
        # If no value is provided, it defaults to English ("en").
        language: "ko"

        # 클라이언트 장치 유형을 선택합니다 ("tv" 또는 "vr"). 기본값은 "vr"입니다.
        # Selects client device type ("tv" or "vr"). Default is "vr".
        client: "vr"
  ```