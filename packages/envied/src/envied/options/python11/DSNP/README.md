# DisneyPlus(디즈니플러스)

```
uv run unshackle dl -vl all -al orig -sl ko,en,ja -q 1080,2160 -v h.264,h.265 -r SDR,HDR10,DV DSNP entity-4d12671a-f0ad-4c3f-8526-09ae6772390b
```

## Information(정보)

- Authorization: Credentials, Web Token
- Security: UHD@L1/SL3000, FHD@L1/SL3000, HD@L3/SL2000
- Working Client Agent: AndroidTV
- Support Codec
  - Video: H264, H265
  - Audio: AAC, AC3, ATMOS, DTS:X(P2:IMAX)
  - Range: SDR, HDR10, HDR10, DV

## Support Args(지원하는 명령어 인자)

- `-i`, `--imax`: Prefer IMAX Enhanced version if available.
- `-r`, `--remastered-ar`: Prefer Remastered Aspect Ratio if available.
- `-e`, `--extras`: Select a extras video if available.
- `-tu`, `--tier-unlimits`: Remove stream quality restrictions for a specific account.

## Tips

- To enable the web refresh token-based login method, please comment out or delete the DSNP section under credentials in `envied.yaml`.
  웹 리프레시 토큰 기반 로그인 방식을 활성화하려면 `envied.yaml`의 credentials에서 DSNP부분을 주석 처리하거나 삭제하세요.

  ```
  credentials:
    ...
    # DSNP: example@example.com:example
  ```

- Configure user settings within the `envied.yaml` file.
  사용자 설정은 `envied.yaml`에서 다음과 같이 사용하세요.

  ```
  services:
    DSNP:
      ## 사용자 환경설정
      ## User configuration
      # 해당 설정값이 주석처리 되어 있는 경우에는 설정값들이 자동으로 선택됩니다.
      # If these settings are commented out, values will be selected automatically.
      preferences:
        # 사용할 프로필의 인덱스 번호를 지정합니다. (0 = 첫 번째 프로필, 1 = 두 번째 프로필 등)
        # Specifies the index of the profile to use. (0 = first profile, 1 = second profile, etc.)
        # 값이 설정되지 않은 경우에는 자동으로 PIN이 안 걸려 있고 키즈 모드가 아닌 프로필로 자동 선택됩니다.
        # If no value is set, a profile without a PIN and not in Kids Mode will be automatically selected.
        profile: 0

        # 서비스 내에서 표시되는 메타데이터 언어를 선택합니다.
        # Selects the metadata language displayed within the service.
        # 언어 설정은 Disney+에서 지원하는 언어 코드(예: "ko", "en")만 사용 가능합니다.
        # Language settings are only available for language codes supported by Disney+ (e.g., "ko", "en").
        # 값이 설정되지 않은 경우에는 현재 프로필에 설정된 언어 설정을 사용합니다.
        # If no value is set, the language settings of the current profile will be used.
        # language: "ko"

        # 매니페스트 로그 출력 레벨을 설정합니다.
        # Sets the manifest log output level.
        # 로그를 항상 표시해야 하는 경우 "info"를 사용하고, 그 외의 모든 경우에는 가급적 "debug"를 사용하십시오.
        # Use "info" if the log must always be displayed; otherwise, use "debug" whenever possible.
        # 값이 설정되지 않은 경우 기본값은 "debug"로 적용됩니다.
        # If no value is set, the default level is "debug".
        # manifest_log: "info"
  ```

- To enable the tier_unlimits command by default, add the following to `envied.yaml`.
  tier_unlimits 명령을 기본값으로 활성화하려면 `envied.yaml`에 다음을 추가하세요.
  ```
  dl:
    ...
    DSNP:
      tier_unlimits: True
  ```
