HTPack 0.5.0b1 — BMOL Windows x64 내부 테스트
기준일: 2026-10-21

이 배포본은 Windows용 Python과 필요한 라이브러리를 포함합니다.
Python을 따로 설치하거나 관리자 권한으로 실행할 필요가 없습니다.
현재 Mac에서 기능 검증을 마쳤으며, 실제 Windows 실행·양방향 교차 복원은
학생들의 테스트 결과를 받은 뒤 확정합니다. 내부 베타입니다.

1. ZIP을 C:\HTPack 처럼 짧고 쓰기 가능한 경로에 완전히 풉니다.
   ZIP 안에서 직접 실행하지 말고, runtime 폴더를 옮기거나 삭제하지 마세요.
2. 해당 폴더에서 PowerShell을 열고 실행합니다.

   .\htpack.cmd --help
   .\htpack.cmd inspect "D:\data\sample.TCF"
   .\htpack.cmd encode "D:\data\sample.TCF" -o "D:\data\sample.htfc"
   .\htpack.cmd encode "D:\data\sample.TCF" -o "D:\data\sample.fast.htfc" --fast
   .\htpack.cmd encode "D:\data\movie.TCF" -o "D:\data\movie.htfc" --ri-error-scale 5
   .\htpack.cmd decode "D:\data\sample.htfc" -o "D:\data\restored.TCF"
   .\htpack.cmd verify "D:\data\sample.htfc" --reference "D:\data\sample.TCF" --restored "D:\data\restored.TCF"

기본: Fourier + 공간 보정 + guard, 최대 RI 오차 1e-4.
--fast: BitRound + Pcodec + 같은 최종 guard.
--ri-error-scale 5 = 5e-4, 0.1 = 1e-5.
time-lapse는 RAM·CPU를 읽어 W를 자동 선택합니다.
--concurrent-volumes N은 상한, --memory-gib N은 보수적인 RAM 계획 상한입니다.
RAM 계획은 OS가 강제하는 메모리 제한이 아닙니다.
기존 출력 파일은 덮어쓰지 않습니다. 다시 실행할 때 새 출력 이름을 사용하세요.

선택 설치: Install.cmd를 실행하면 동일 배포본을 사용자 LOCALAPPDATA 아래로
복사하고 설치 경로를 출력합니다. PATH와 레지스트리는 변경하지 않습니다.
압축을 푼 폴더에서 바로 쓰는 방식도 지원합니다.

학생 검증 절차

1. 별도의 htpack-0.5.0b1-validation-data.zip도 풉니다.
   validation-data 안에 data-manifest.json, 3D TCF, 10시점 TCF,
   Mac에서 만든 각 모드의 압축 파일 4개가 있습니다.
2. 다른 프로그램의 과도한 RAM 사용을 줄이고 다음을 실행합니다.

   .\Run-Tests.cmd
   .\Validate-Windows.cmd --data "D:\validation-data"

   필요하면 --output "D:\HTPack-results" --memory-gib 4를 추가할 수 있습니다.
   데이터·복원 파일을 위한 여유 디스크 공간 6 GB 이상을 권장합니다.
3. 마지막에 표시되는 .return.zip과 validation\pytest-windows.xml을 돌려주세요.
   .return.zip에는 하드웨어 정보·검사 로그·Windows 생성 압축 파일이 들어갑니다.
   큰 복원 TCF 파일은 포함하지 않습니다.
   실패하더라도 return ZIP을 돌려주면 실패 위치를 확인할 수 있습니다.
4. 가능하면 TomoAnalysis의 새 세션에서 원본과 restored 폴더의 복원 TCF를
   각각 열어 전체 Z와 time point 이동, RI 및 BF/FL 로딩을 확인해 주세요.
   동일 UniqueID를 보존하므로 기존에 열린 원본이나 MIP만 보고 판단하지 마세요.
   앱 버전과 성공/실패, 오류 메시지를 함께 알려주세요.

검증기는 기본 오차 1e-4의 전체 voxel, float32 RI reader, 속성의 값·형식·차원,
시간 순서, 비대상 dataset의 값·저장 설정을 확인합니다.
Mac→Windows는 이 실행으로 확인하고, Windows→Mac은 돌아온 압축 파일을
Mac에서 실제 복원한 뒤 별도로 확인합니다.

지원 대상: TCF 1.3.6/1.4.1/1.6.3의 지원 scalar RI 프로파일.
모든 TCF 변형·Windows ARM64·32 bit·모든 Windows 버전의 호환을 주장하지 않습니다.
현재 QAT/iDTT 입력은 이 CLI의 지원 범위가 아닙니다.
상세 형식과 제약은 FORMAT.txt, 빌드 출처·해시는 build-provenance.json,
파일 무결성 목록은 bundle-manifest.json을 참조하세요.
동봉한 제3자 라이브러리의 라이선스와 고지는 각 runtime dist-info 등에 보존했습니다.
HTPack 자체는 현 단계 BMOL 내부 검증용이며 공개 배포 라이선스는 미정입니다.
