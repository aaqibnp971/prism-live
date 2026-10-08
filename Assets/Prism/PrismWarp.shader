// Prism Live: a softly winding tunnel of light you travel through (GPU only, one draw call).
// Streaks sit on the tunnel wall; faint rings mark the distance so it feels like moving inside.
// Everything is computed from one shared "travel" distance, so speed changes are always smooth.
Shader "Prism/Warp"
{
    Properties { }
    SubShader
    {
        Tags { "Queue"="Transparent" "RenderType"="Transparent" "IgnoreProjector"="True" }
        Blend One One
        ZWrite Off
        Cull Off

        Pass
        {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #pragma target 3.0
            #include "UnityCG.cginc"

            // No heartbeat input here on purpose. The streaks fill the whole view, and the
            // flashing-light rule forbids a full-field pulse; only PrismSpace's glow may brighten.
            float _Travel, _Span, _Far, _Len, _Width, _Density;
            float _Twist, _Spin, _CurveAmp, _CurveK, _RingArc, _RingBright;
            float4 _Color;

            struct appdata
            {
                float4 vertex : POSITION;
                float2 uv : TEXCOORD0;      // x: side -1/+1, y: 0 = lead end, 1 = tail end
                float4 p : TEXCOORD1;       // angle, radius, z0 (0..1), seed (0..1)
                float2 k : TEXCOORD2;       // x: 0 = streak, 1 = ring segment
                UNITY_VERTEX_INPUT_INSTANCE_ID
            };
            struct v2f
            {
                float4 pos : SV_POSITION;
                float side : TEXCOORD0;
                float4 col : TEXCOORD1;
                UNITY_VERTEX_OUTPUT_STEREO
            };

            // centre line of the tunnel, zero at the viewer so the tube always starts around you
            float2 bend(float u)
            {
                float2 a = float2(sin(_CurveK * u), sin(_CurveK * 0.73 * u + 1.7));
                float2 b = float2(sin(_CurveK * _Travel), sin(_CurveK * 0.73 * _Travel + 1.7));
                return (a - b) * _CurveAmp;
            }

            float3 wall(float th, float r, float z)
            {
                float u = z + _Travel;                       // fixed tunnel coordinate
                float ang = th + _Twist * u + _Spin;          // gentle spiral
                float2 c = bend(u);
                return float3(cos(ang) * r + c.x, sin(ang) * r + c.y, z);
            }

            v2f vert (appdata v)
            {
                v2f o;
                UNITY_SETUP_INSTANCE_ID(v);
                UNITY_INITIALIZE_OUTPUT(v2f, o);
                UNITY_INITIALIZE_VERTEX_OUTPUT_STEREO(o);

                float th = v.p.x, r = v.p.y;
                float z = _Far - fmod(v.p.z * _Span + _Travel, _Span);       // far → past the viewer
                bool ring = v.k.x > 0.5;

                float3 lead = wall(th, r, z);
                float3 tail = ring ? wall(th + _RingArc, r, z) : wall(th, r, z + _Len);

                float3 vL = UnityObjectToViewPos(lead);
                float3 vT = UnityObjectToViewPos(tail);
                float3 vp = lerp(vL, vT, v.uv.y);

                float2 d = vT.xy / max(-vT.z, 0.05) - vL.xy / max(-vL.z, 0.05);
                float2 perp = length(d) > 1e-5 ? normalize(float2(-d.y, d.x)) : float2(1, 0);
                float w = max(_Width, -vp.z * 0.0012);
                vp.xy += perp * v.uv.x * w;

                float fade = smoothstep(_Far, _Far * 0.55, z) * smoothstep(-8.0, -1.0, z);
                float visible = v.p.w <= _Density ? 1.0 : 0.0;
                float along = ring ? _RingBright : lerp(1.0, 0.04, v.uv.y);

                o.col = _Color * (fade * visible * along);
                o.pos = mul(UNITY_MATRIX_P, float4(vp, 1.0));
                o.side = v.uv.x;
                return o;
            }

            float4 frag (v2f i) : SV_Target
            {
                float edge = 1.0 - abs(i.side);
                return i.col * edge * edge;
            }
            ENDCG
        }
    }
    Fallback Off
}
