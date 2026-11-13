; =============================================================================
; 舆见 OpinionLens — NSIS 覆盖宏（electron-builder include）
;
; 目标：
; 1) 安装/升级前可靠结束本应用进程与历史遗留卸载器，避免安装目录文件被占用；
; 2) 升级时跳过旧版本卸载器：
;    electron-builder 默认会 exec 旧 UninstallString；旧卸载器在部分机器上会卡死
;    或返回非 0，导致新安装器报 "Failed to uninstall old application files" 并中止
;    （用户侧表现就是“安装失败”）。preInit 在 .onInit 最前面执行，此时 NSIS 仍是
;    32 位注册表视图，而旧安装信息写在 64 位视图——必须显式 SetRegView 后再删，
;    否则删除无效（这正是此前修复不生效的原因）。
; 3) 对旧卸载器结果做兜底忽略，保证升级不因旧卸载器问题被阻塞。
; =============================================================================

; 删除旧版本的“卸载登记”（32/64 位视图都要清，确保 uninstallOldVersion 找不到旧卸载器）
!macro fsDeleteUninstallKeys
  DeleteRegKey HKCU "${UNINSTALL_REGISTRY_KEY}"
  DeleteRegKey HKLM "${UNINSTALL_REGISTRY_KEY}"
  !ifdef UNINSTALL_REGISTRY_KEY_2
    DeleteRegKey HKCU "${UNINSTALL_REGISTRY_KEY_2}"
    DeleteRegKey HKLM "${UNINSTALL_REGISTRY_KEY_2}"
  !endif
!macroend

!macro customCheckAppRunning
  nsExec::Exec 'taskkill /F /T /IM "OpinionLens.exe"'
  Pop $0
  ; 异常中断/旧版本升级可能遗留旧卸载器进程，占用安装目录文件
  nsExec::Exec 'taskkill /F /IM "old-uninstaller.exe"'
  Pop $0
  nsExec::Exec 'taskkill /F /IM "Un_A.exe"'
  Pop $0
  Sleep 400
!macroend

!macro preInit
  ; 先读出旧安装目录（用于清理遗留卸载器文件），再删除卸载登记
  SetRegView 64
  ReadRegStr $R8 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
  ReadRegStr $R9 HKLM "${INSTALL_REGISTRY_KEY}" InstallLocation
  !insertmacro fsDeleteUninstallKeys
  SetRegView 32
  !insertmacro fsDeleteUninstallKeys
  SetRegView 64
  ${If} $R8 != ""
    Delete "$R8\Uninstall OpinionLens.exe"
  ${EndIf}
  ${If} $R9 != ""
    Delete "$R9\Uninstall OpinionLens.exe"
  ${EndIf}
  ; 非提权安装时删除 HKLM 登记会失败并置错误标记；清掉，避免影响后续流程
  ClearErrors
!macroend

; 如果旧卸载器仍然返回非 0，不让安装流程失败（新安装会覆盖文件）
!macro customUnInstallCheck
  ClearErrors
  StrCpy $R0 0
!macroend

!macro customUnInstallCheckCurrentUser
  ClearErrors
  StrCpy $R0 0
!macroend
