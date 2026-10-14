# U-NEXT(유넥스트)

```
uv run unshackle dl --list -vl all -al orig -sl all -q 2160 -v h.265 -r HDR10 UNXT SID0087607
```

## Information(정보)

- Authorization: Credentials
- Security: UHD@L3/SL2000, FHD@L3/SL2000
- Working Client Agent: AndroidTV
- Support Codec
  - Video: H264, H265
  - Audio: AAC, AC3, ATMOS
  - Range: SDR, HDR10, DV

## Support Args(지원하는 명령어 인자)

- `-t`, `--type`: Prefer subtitle or dubbing version for content type if available.

## Tips

- Configure user settings within the `envied.yaml` file.
  사용자 설정은 `envied.yaml`에서 다음과 같이 사용하세요.
  
  ```
  services:
    ## 사용자 환경설정
    ## User configuration
    # 해당 설정값이 주석처리 되어 있는 경우에는 설정값들이 자동으로 선택됩니다.
    # If these settings are commented out, values will be selected automatically.
    preferences:
      # 매니페스트 로그 출력 레벨을 설정합니다.
      # Sets the manifest log output level.
      # 로그를 항상 표시해야 하는 경우 "info"를 사용하고, 그 외의 모든 경우에는 가급적 "debug"를 사용하십시오.
      # Use "info" if the log must always be displayed; otherwise, use "debug" whenever possible.
      # 값이 설정되지 않은 경우 기본값은 "debug"로 적용됩니다.
      # If no value is set, the default level is "debug".
      manifest_log: "debug"
  ```
