// dusk_dsp_shim.cpp — 把 duskaudio::TapeMachineDSP 暴露成扁平 C 接口，供 Python(ctypes) 调用。
//
// 上游：dusk-audio/dusk-audio-plugins 的 TapeMachine core（GPL-3.0-or-later，见 ../LICENSE-GPL-3.0.txt）。
// 本文件不含上游代码，只做接口扁平化 + 交错/平面转换 + 分块调用。
// 构建：tools/build_dusk_dsp.ps1（vcvars64 + cl，无需 CMake / JUCE / ImGui）。
//
// 音频数据一律用 **交错 float32**（与 Python 侧 numpy 的 s16le→float32 视图对应），
// 通道数只支持 2（立体声）；其它通道数原样透传，绝不阻塞调用方。

#define DUSK_EXPORT extern "C" __declspec(dllexport)

#include "TapeMachineDSP.hpp"

#include <cstring>
#include <vector>

namespace
{
struct Host
{
    duskaudio::TapeMachineDSP dsp;
    std::vector<float> inL, inR, outL, outR;
    int maxBlock = 0;
    bool ready = false;
};
} // namespace

// ---- 参数 id：必须与 app/tape_fx.py 的 PARAMS 表逐项对应（改动时两边一起改）----
enum
{
    P_MACHINE = 0, P_SPEED, P_TYPE, P_SIGNAL_PATH, P_EQ_STANDARD,
    P_INPUT_GAIN_DB, P_BIAS, P_CALIBRATION, P_AUTO_CAL,
    P_HIGHPASS_HZ, P_LOWPASS_HZ, P_NOISE, P_WOW, P_FLUTTER,
    P_OUTPUT_GAIN_DB, P_AUTO_COMP, P_OVERSAMPLING, P_HEAD_WIDTH,
    P_CROSSTALK, P_WOWFLUTTER_ON, P_TRANSFORMER,
    P_REPRO_LF, P_REPRO_LMF, P_REPRO_HMF, P_REPRO_HF, P_REPRO_SUB,
    P_LEVEL_HMF_TRIM, P_LEVEL_HF_TRIM, P_LP_Q,
    P_BYPASS,
    P_ACTIVE,                     // 我们自己加的软开关（= !bypass，便于 UI 表达 POWER）
    P_METER_VU_L = 100, P_METER_VU_R,
    P_METER_IN_PEAK_L, P_METER_IN_PEAK_R,
    P_METER_OUT_PEAK_L, P_METER_OUT_PEAK_R,
    P_INFO_LATENCY = 200,
};

DUSK_EXPORT void* dusk_create()
{
    return new Host();
}

DUSK_EXPORT void dusk_destroy(void* h)
{
    delete static_cast<Host*>(h);
}

DUSK_EXPORT void dusk_prepare(void* h, double sampleRate, int maxBlock)
{
    auto* p = static_cast<Host*>(h);
    if (p == nullptr)
        return;
    if (maxBlock < 1)
        maxBlock = 512;
    if (sampleRate < 8000.0)
        sampleRate = 48000.0;
    p->maxBlock = maxBlock;
    p->dsp.prepare(sampleRate, maxBlock);
    p->dsp.reset();
    p->inL.assign(static_cast<size_t>(maxBlock), 0.0f);
    p->inR.assign(static_cast<size_t>(maxBlock), 0.0f);
    p->outL.assign(static_cast<size_t>(maxBlock), 0.0f);
    p->outR.assign(static_cast<size_t>(maxBlock), 0.0f);
    p->ready = true;
}

DUSK_EXPORT void dusk_reset(void* h)
{
    if (auto* p = static_cast<Host*>(h))
        p->dsp.reset();
}

DUSK_EXPORT int dusk_latency(void* h)
{
    if (auto* p = static_cast<Host*>(h))
        return p->dsp.latencySamples();
    return 0;
}

DUSK_EXPORT void dusk_set(void* h, int id, float v)
{
    auto* p = static_cast<Host*>(h);
    if (p == nullptr)
        return;
    auto& d = p->dsp;
    switch (id)
    {
        case P_MACHINE:         d.setTapeMachine(static_cast<int>(v)); break;
        case P_SPEED:           d.setTapeSpeed(static_cast<int>(v)); break;
        case P_TYPE:            d.setTapeType(static_cast<int>(v)); break;
        case P_SIGNAL_PATH:     d.setSignalPath(static_cast<int>(v)); break;
        case P_EQ_STANDARD:     d.setEqStandard(static_cast<int>(v)); break;
        case P_INPUT_GAIN_DB:   d.setInputGainDb(v); break;
        case P_BIAS:            d.setBias(v); break;
        case P_CALIBRATION:     d.setCalibration(static_cast<int>(v)); break;
        case P_AUTO_CAL:        d.setAutoCal(v > 0.5f); break;
        case P_HIGHPASS_HZ:     d.setHighpassHz(v); break;
        case P_LOWPASS_HZ:      d.setLowpassHz(v); break;
        case P_NOISE:           // core 里噪声有个独立开关且默认关闭（pNoiseEnabled=false），
                                // 只设量而不开开关的话，NOISE 旋钮永远没有任何效果。
                                d.setNoiseAmount(v);
                                d.setNoiseEnabled(v > 0.001f);
                                break;
        case P_WOW:             d.setWow(v); break;
        case P_FLUTTER:         d.setFlutter(v); break;
        case P_OUTPUT_GAIN_DB:  d.setOutputGainDb(v); break;
        case P_AUTO_COMP:       d.setAutoComp(v > 0.5f); break;
        case P_OVERSAMPLING:    d.setOversampling(static_cast<int>(v)); break;
        case P_HEAD_WIDTH:      d.setHeadWidth(static_cast<int>(v)); break;
        case P_CROSSTALK:       d.setCrosstalk(v > 0.5f); break;
        case P_WOWFLUTTER_ON:   d.setWowFlutterEnabled(v > 0.5f); break;
        case P_TRANSFORMER:     d.setTransformer(v > 0.5f); break;
        case P_REPRO_LF:        d.setReproLf(v); break;
        case P_REPRO_LMF:       d.setReproLmf(v); break;
        case P_REPRO_HMF:       d.setReproHmf(v); break;
        case P_REPRO_HF:        d.setReproHf(v); break;
        case P_REPRO_SUB:       d.setReproSubBell(v); break;
        case P_LEVEL_HMF_TRIM:  d.setLevelHmfTrim(v); break;
        case P_LEVEL_HF_TRIM:   d.setLevelHfTrim(v); break;
        case P_LP_Q:            d.setLpQ(v); break;
        case P_BYPASS:          d.setBypass(v > 0.5f); break;
        case P_ACTIVE:          d.setBypass(v <= 0.5f); break;   // ACTIVE=1 → 不 bypass
        default: break;
    }
}

DUSK_EXPORT float dusk_get(void* h, int id)
{
    auto* p = static_cast<Host*>(h);
    if (p == nullptr)
        return 0.0f;
    auto& d = p->dsp;
    switch (id)
    {
        case P_METER_VU_L:        return d.getVuL();
        case P_METER_VU_R:        return d.getVuR();
        case P_METER_IN_PEAK_L:   return d.getInPeakL();
        case P_METER_IN_PEAK_R:   return d.getInPeakR();
        case P_METER_OUT_PEAK_L:  return d.getOutPeakL();
        case P_METER_OUT_PEAK_R:  return d.getOutPeakR();
        case P_INFO_LATENCY:      return static_cast<float>(d.latencySamples());
        default: return 0.0f;
    }
}

// in/out 均为交错 float32；frames 可大于 prepare 时的 maxBlock，内部自动分块。
DUSK_EXPORT void dusk_process(void* h, const float* in, float* out, int frames, int channels)
{
    auto* p = static_cast<Host*>(h);
    if (p == nullptr || in == nullptr || out == nullptr || frames <= 0)
        return;
    if (channels != 2 || !p->ready || p->maxBlock < 1)
    {
        std::memcpy(out, in, sizeof(float) * static_cast<size_t>(frames) * static_cast<size_t>(channels > 0 ? channels : 1));
        return;
    }

    int done = 0;
    while (done < frames)
    {
        const int n = (frames - done) < p->maxBlock ? (frames - done) : p->maxBlock;
        for (int i = 0; i < n; ++i)
        {
            p->inL[static_cast<size_t>(i)] = in[static_cast<size_t>(done + i) * 2u];
            p->inR[static_cast<size_t>(i)] = in[static_cast<size_t>(done + i) * 2u + 1u];
        }
        const float* ip[2] = { p->inL.data(), p->inR.data() };
        float* op[2] = { p->outL.data(), p->outR.data() };
        p->dsp.processBlock(ip, op, 2, n);
        for (int i = 0; i < n; ++i)
        {
            out[static_cast<size_t>(done + i) * 2u] = p->outL[static_cast<size_t>(i)];
            out[static_cast<size_t>(done + i) * 2u + 1u] = p->outR[static_cast<size_t>(i)];
        }
        done += n;
    }
}
