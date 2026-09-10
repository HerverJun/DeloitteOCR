// OS-level, per-executable network denial for acceptance only.
// Dynamic WFP filters disappear automatically when this process exits.
// No adapter, firewall profile, registry or unrelated application is modified.
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <fwpmu.h>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>
#pragma comment(lib,"Fwpuclnt.lib")
#pragma comment(lib,"Ws2_32.lib")
#pragma comment(lib,"Advapi32.lib")

struct EngineHandle {
    HANDLE value = nullptr;
    ~EngineHandle() { if (value) FwpmEngineClose0(value); }
};

int wmain(int argc, wchar_t** argv) {
    if (argc == 2 && std::wstring(argv[1]) == L"--probe") {
        WSADATA wsa{};
        if (WSAStartup(MAKEWORD(2,2), &wsa)) return 3;
        SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        u_long nonblocking = 1; ioctlsocket(sock, FIONBIO, &nonblocking);
        sockaddr_in addr{}; addr.sin_family = AF_INET; addr.sin_port = htons(443);
        addr.sin_addr.s_addr = htonl(0x01010101);
        int result = connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
        int error = result == SOCKET_ERROR ? WSAGetLastError() : 0;
        if (error == WSAEWOULDBLOCK) {
            fd_set writes, errors; FD_ZERO(&writes); FD_ZERO(&errors);
            FD_SET(sock,&writes); FD_SET(sock,&errors); timeval timeout{4,0};
            if (select(0,nullptr,&writes,&errors,&timeout) > 0) {
                int length=sizeof(error);
                getsockopt(sock,SOL_SOCKET,SO_ERROR,reinterpret_cast<char*>(&error),&length);
            } else error=WSAETIMEDOUT;
        }
        closesocket(sock); WSACleanup();
        std::cout << "{\"native_connect_error\":" << error << "}\n";
        return error == WSAEACCES ? 0 : 4;
    }
    if (argc != 3) {
        std::wcerr << L"Usage: offline_guard.exe BUNDLE OUTPUT (requires Administrator)\n";
        return 2;
    }
    namespace fs = std::filesystem;
    const fs::path root = fs::absolute(argv[1]);
    const fs::path output = fs::absolute(argv[2]);
    if (!fs::is_regular_file(root / L"runtimes/control/python.exe")) return 2;
    std::vector<fs::path> programs;
    for (auto name : {L"control",L"ppocr",L"paddlevl",L"glm",L"hunyuan"})
        programs.push_back(root/L"runtimes"/name/L"python.exe");
    programs.push_back(root/L"runtimes/llama/llama-server.exe");
    programs.push_back(root/L"tools/offline_guard.exe");
    for (const auto& path : programs) if (!fs::is_regular_file(path)) {
        std::wcerr << L"Missing executable: " << path << L"\n"; return 2;
    }
    EngineHandle engine;
    FWPM_SESSION0 session{};
    session.flags = FWPM_SESSION_FLAG_DYNAMIC;
    DWORD status = FwpmEngineOpen0(nullptr, RPC_C_AUTHN_WINNT, nullptr, &session, &engine.value);
    if (status != ERROR_SUCCESS) {
        std::cerr << "WFP open failed: " << status << " (Administrator required)\n"; return 5;
    }
    UINT64 count = 0;
    for (const auto& path : programs) {
        FWP_BYTE_BLOB* app = nullptr;
        status = FwpmGetAppIdFromFileName0(path.c_str(), &app);
        if (status != ERROR_SUCCESS) return 6;
        for (auto layer : {FWPM_LAYER_ALE_AUTH_CONNECT_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V6}) {
            FWPM_FILTER_CONDITION0 conditions[2]{};
            conditions[0].fieldKey = FWPM_CONDITION_ALE_APP_ID;
            conditions[0].matchType = FWP_MATCH_EQUAL;
            conditions[0].conditionValue.type = FWP_BYTE_BLOB_TYPE;
            conditions[0].conditionValue.byteBlob = app;
            conditions[1].fieldKey = FWPM_CONDITION_FLAGS;
            conditions[1].matchType = FWP_MATCH_FLAGS_NONE_SET;
            conditions[1].conditionValue.type = FWP_UINT32;
            conditions[1].conditionValue.uint32 = FWP_CONDITION_FLAG_IS_LOOPBACK;
            FWPM_FILTER0 filter{};
            filter.displayData.name = const_cast<wchar_t*>(L"OfflineOCR temporary acceptance isolation");
            filter.layerKey = layer;
            filter.subLayerKey = FWPM_SUBLAYER_UNIVERSAL;
            filter.weight.type = FWP_UINT8; filter.weight.uint8 = 15;
            filter.action.type = FWP_ACTION_BLOCK;
            filter.numFilterConditions = 2; filter.filterCondition = conditions;
            UINT64 id = 0;
            status = FwpmFilterAdd0(engine.value, &filter, nullptr, &id);
            if (status != ERROR_SUCCESS) {
                FwpmFreeMemory0(reinterpret_cast<void**>(&app));
                std::cerr << "WFP filter failed: " << status << "\n"; return 7;
            }
            ++count;
        }
        FwpmFreeMemory0(reinterpret_cast<void**>(&app));
    }
    std::cout << "Installed " << count << " temporary WFP filters; loopback remains available.\n";
    const fs::path python = root/L"runtimes/control/python.exe";
    std::wstring command = L"\"" + python.wstring() + L"\" -X utf8 -I -m ocr_workbench.airgap --bundle \"" +
                          root.wstring() + L"\" --output \"" + output.wstring() + L"\"";
    STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    if (!job) return 8;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
        CloseHandle(job); return 8;
    }
    if (!CreateProcessW(python.c_str(), command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW|CREATE_SUSPENDED,
                        nullptr, root.c_str(), &startup, &process)) return 8;
    if (!AssignProcessToJobObject(job, process.hProcess)) {
        TerminateProcess(process.hProcess, 8); CloseHandle(process.hThread); CloseHandle(process.hProcess);
        CloseHandle(job); return 8;
    }
    ResumeThread(process.hThread);
    CloseHandle(process.hThread);
    if (WaitForSingleObject(process.hProcess, 45*60*1000) != WAIT_OBJECT_0) {
        TerminateJobObject(job, 9); WaitForSingleObject(process.hProcess, 10000);
    }
    DWORD code = 1; GetExitCodeProcess(process.hProcess, &code); CloseHandle(process.hProcess);
    CloseHandle(job);
    std::cout << "Acceptance exit code " << code << "; temporary filters removed on exit.\n";
    return static_cast<int>(code);
}
