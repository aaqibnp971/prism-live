// Prism Live field, drawn around the viewer as a skybox.
// A direct port of the team's own renderer: web/shared/field_renderer.js (same maths, same
// colours, same fog/streak/light layers, same pulse safety cap). The only addition is the
// mapping from a VR view direction to the renderer's screen coordinates (uv, top-origin).
Shader "Prism/Sky"
{
    Properties { }
    SubShader
    {
        Tags { "Queue"="Background" "RenderType"="Background" "PreviewType"="Skybox" }
        Cull Off ZWrite Off

        Pass
        {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #pragma target 3.0
            #include "UnityCG.cginc"

            // layer 0 = previous palette, layer 1 = current palette
            float4 _A0, _B0, _A1, _B1;   // A: fog, light, hue, sat   B: horizon, pulse, motion, weight
            float  _Env;                 // heartbeat envelope 0..1
            float  _DriftPx;             // drift in reference pixels, wrapped to 0..40
            float4 _Size;                // reference width, height (pixels)
            float4 _Fov;                 // x = view width degrees, y = view height degrees

            struct appdata { float4 vertex : POSITION; UNITY_VERTEX_INPUT_INSTANCE_ID };
            struct v2f { float4 pos : SV_POSITION; float3 dir : TEXCOORD0; UNITY_VERTEX_OUTPUT_STEREO };

            v2f vert (appdata v)
            {
                v2f o;
                UNITY_SETUP_INSTANCE_ID(v);
                UNITY_INITIALIZE_OUTPUT(v2f, o);
                UNITY_INITIALIZE_VERTEX_OUTPUT_STEREO(o);
                o.pos = UnityObjectToClipPos(v.vertex);
                o.dir = v.vertex.xyz;
                return o;
            }

            float alpha1(float x) { return floor(x * 1000.0 + 0.5) / 1000.0; }
            float lin(float x) { return x <= 0.04045 ? x / 12.92 : pow((x + 0.055) / 1.055, 2.4); }
            float3 linearRGB(float3 x) { return float3(lin(x.r), lin(x.g), lin(x.b)); }
            float enc(float x) { return x <= 0.0031308 ? 12.92 * x : 1.055 * pow(x, 1.0 / 2.4) - 0.055; }
            float3 encodedRGB(float3 x) { return saturate(float3(enc(x.r), enc(x.g), enc(x.b))); }
            float lum(float3 c) { return dot(c, float3(0.2126, 0.7152, 0.0722)); }

            float3 hsv(float h, float s, float v)
            {
                float c = v * s;
                float x = c * (1.0 - abs(fmod(h / 60.0, 2.0) - 1.0));
                float m = v - c;
                float3 rgb;
                if (h < 60.0) rgb = float3(c, x, 0.0);
                else if (h < 120.0) rgb = float3(x, c, 0.0);
                else if (h < 180.0) rgb = float3(0.0, c, x);
                else if (h < 240.0) rgb = float3(0.0, x, c);
                else if (h < 300.0) rgb = float3(x, 0.0, c);
                else rgb = float3(c, 0.0, x);
                return floor(saturate(rgb + m) * 255.0 + 0.5) / 255.0;
            }

            float ramp(float x, float a, float b) { return saturate((x - a) / (b - a)); }

            float3 baseField(float4 a, float4 b, float2 uv)
            {
                float V = .30 + a.y * .44; float H = b.x;
                float3 c0 = hsv(a.z, a.w, V - .07), c1 = hsv(a.z, a.w * .9, V);
                float3 c2 = hsv(a.z, a.w * 1.25, V - .15), c3 = hsv(a.z, a.w * 1.3, V - .24);
                if (uv.y <= H - .012) return lerp(c0, c1, ramp(uv.y, 0.0, H - .012));
                if (uv.y <= H + .012) return lerp(c1, c2, ramp(uv.y, H - .012, H + .012));
                return lerp(c2, c3, ramp(uv.y, H + .012, 1.0));
            }

            float band(float p)
            {
                float x = p - 40.0 * floor(p / 40.0);
                if (x <= 9.0) return x / 9.0;
                if (x <= 18.0) return 1.0 - (x - 9.0) / 9.0;
                if (x <= 31.0) return (x - 18.0) / 13.0 * .5;
                return .5 * (1.0 - (x - 31.0) / 9.0);
            }

            float3 field(float4 a, float4 b, float2 uv, float3 base, float pulse)
            {
                float V = .30 + a.y * .44, H = b.x, k = b.y * pulse;
                float fa = min(.95, a.x * (1.0 + k * .5));
                float3 fogC = hsv(a.z, a.w * .45, min(.97, (V + .2) * (1.0 + k * .6)));
                float3 color = lerp(base, hsv(a.z, a.w * .45, min(.97, V + .2)), alpha1(min(.95, a.x) * .32));
                float fy = uv.y - H;
                float f;
                if (fy <= -.08) f = lerp(0.0, alpha1(fa * .55), ramp(fy, -.34, -.08));
                else if (fy <= 0.0) f = lerp(alpha1(fa * .55), alpha1(fa), ramp(fy, -.08, 0.0));
                else if (fy <= .10) f = lerp(alpha1(fa), alpha1(fa * .7), ramp(fy, 0.0, .10));
                else f = lerp(alpha1(fa * .7), 0.0, ramp(fy, .10, .34));
                float sa = alpha1(min(.5, b.z * 7.0) * fa);
                float len = _Size.x * .017452406 + _Size.y * .999847695;
                float pos = (uv.x - .5) * _Size.x * .017452406 + (uv.y - .5) * _Size.y * .999847695 + len * .5 + _DriftPx;
                float bands = 0.0, norm = 0.0;
                [unroll] for (int j = -4; j <= 4; j++)
                {
                    float q = (float)j; float w = exp(-.5 * q * q / (1.25 * 1.25));
                    bands += w * band(pos + q * 2.0); norm += w;
                }
                float mask = min(ramp(fy, -.22, -.04), 1.0 - ramp(fy, .06, .20));
                color = lerp(color, fogC, sa * bands / norm * mask);
                color = lerp(color, fogC, f);
                float r = length((uv - float2(.5, .4)) / float2(.58, .46));
                float la = min(1.0, a.y * .78 * (1.0 + k));
                float l;
                if (r <= .30) l = lerp(alpha1(la), alpha1(la * .55), ramp(r, 0.0, .30));
                else if (r <= .60) l = lerp(alpha1(la * .55), alpha1(la * .18), ramp(r, .30, .60));
                else l = lerp(alpha1(la * .18), 0.0, ramp(r, .60, 1.0));
                return lerp(color, hsv(a.z, a.w * .35, min(.98, V + .28 + k * .1)), l);
            }

            void addLayer(float4 a, float4 b, float2 uv, inout float3 rest, inout float3 candidate, inout float budget)
            {
                if (b.w <= 0.0) return;
                float3 base = baseField(a, b, uv);
                float3 r = linearRGB(field(a, b, uv, base, 0.0));
                float3 delta = max(0.0, linearRGB(field(a, b, uv, base, 1.0)) - r);
                float cap = min(b.y * lum(linearRGB(base)), .09);
                float scale = min(1.0, cap / max(1e-12, lum(delta)));
                rest += b.w * r; candidate += b.w * (r + delta * scale); budget += b.w * cap;
            }

            float4 frag (v2f i) : SV_Target
            {
                // VR direction → the renderer's screen coordinates (top-origin, 0.5 = straight ahead)
                float3 d = normalize(i.dir);
                float az = atan2(d.x, d.z);
                float el = asin(clamp(d.y, -1.0, 1.0));
                float2 uv = float2(0.5 + degrees(az) / _Fov.x, 0.5 - degrees(el) / _Fov.y);

                float3 rest = 0, candidate = 0; float budget = 0;
                addLayer(_A0, _B0, uv, rest, candidate, budget);
                addLayer(_A1, _B1, uv, rest, candidate, budget);

                float3 restQ = floor(encodedRGB(rest) * 255.0 + .5) / 255.0;
                float3 rq = linearRGB(restQ);
                float3 delta = max(0.0, candidate - rq);
                float scale = min(1.0, budget / max(1e-12, lum(delta)));
                float3 outEnc = max(restQ, floor(encodedRGB(rq + delta * scale * _Env) * 255.0 + .000001) / 255.0);
                float3 shown = _Env <= 0.0 ? restQ : outEnc;

                // the project renders in linear colour space; Unity re-encodes to sRGB on output
                return float4(linearRGB(shown), 1.0);
            }
            ENDCG
        }
    }
    Fallback Off
}
