// Prism Live: the space the streaks drift through. Deep night blue (never pure black,
// which glares on the Quest lenses), a soft glow where the streaks come from, and faint
// fixed stars that never move, giving the eyes a stable anchor (reduces motion sickness).
Shader "Prism/Space"
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

            float4 _Tint;       // linear colour from the current mood
            float _Bright;
            float _PulseGain;   // heartbeat amplitude x envelope, 0..0.22 (PrismPulseSafety.PulseGain)

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

            float hash(float3 p)
            {
                p = frac(p * 0.3183099 + 0.1);
                p *= 17.0;
                return frac(p.x * p.y * p.z * (p.x + p.y + p.z));
            }

            float4 frag (v2f i) : SV_Target
            {
                float3 d = normalize(i.dir);
                float3 col = float3(0.006, 0.008, 0.018) + _Tint.rgb * 0.012;   // deep night blue floor

                // soft glow where the streaks come from (straight ahead)
                float c = saturate(d.z);
                float glow = pow(c, 6.0) * 0.10 + pow(c, 30.0) * 0.18;
                float3 glowC = _Tint.rgb * glow * _Bright;
                col += glowC;

                // Heartbeat, under the flashing-light rules (PrismPulseSafety.LuminanceIncrease):
                // only this glow brightens, by at most _PulseGain (<= 0.22) of its own light and
                // never more than 0.09 linear luminance. The night floor, the stars, the streaks
                // and everything behind the viewer stay exactly as they are.
                float glowL = dot(glowC, float3(0.2126, 0.7152, 0.0722));
                float gain = clamp(_PulseGain, 0.0, 0.22);
                col += glowC * (min(glowL * gain, 0.09) / max(glowL, 1e-6));

                // faint fixed stars
                float3 cell = floor(d * 140.0);
                float h = hash(cell);
                float3 centre = (cell + 0.5 + (float3(hash(cell + 1.3), hash(cell + 2.7), hash(cell + 4.1)) - 0.5) * 0.6) / 140.0;
                float dist = length(d - normalize(centre)) * 140.0;
                float star = step(0.985, h) * saturate(1.0 - dist * 2.2) * 0.35;
                col += star * (0.6 + 0.4 * _Tint.rgb);

                return float4(col, 1.0);
            }
            ENDCG
        }
    }
    Fallback Off
}
