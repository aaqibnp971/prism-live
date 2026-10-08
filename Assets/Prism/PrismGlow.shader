// Prism Live: a soft glowing dot (game target and reticle). Unlit, additive, always visible.
Shader "Prism/Glow"
{
    Properties
    {
        _Color ("Color", Color) = (1, 1, 1, 1)
        _Intensity ("Intensity", Float) = 1
    }
    SubShader
    {
        Tags { "Queue"="Transparent+10" "RenderType"="Transparent" "IgnoreProjector"="True" }
        Blend One One
        ZWrite Off
        ZTest Always
        Cull Off

        Pass
        {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "UnityCG.cginc"

            float4 _Color;
            float _Intensity;

            struct appdata { float4 vertex : POSITION; float2 uv : TEXCOORD0; UNITY_VERTEX_INPUT_INSTANCE_ID };
            struct v2f { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; UNITY_VERTEX_OUTPUT_STEREO };

            v2f vert (appdata v)
            {
                v2f o;
                UNITY_SETUP_INSTANCE_ID(v);
                UNITY_INITIALIZE_OUTPUT(v2f, o);
                UNITY_INITIALIZE_VERTEX_OUTPUT_STEREO(o);
                o.pos = UnityObjectToClipPos(v.vertex);
                o.uv = v.uv;
                return o;
            }

            float4 frag (v2f i) : SV_Target
            {
                float r = length(i.uv - 0.5) * 2.0;
                float core = smoothstep(0.35, 0.0, r);
                float halo = pow(saturate(1.0 - r), 3.0) * 0.6;
                return _Color * _Intensity * (core + halo);
            }
            ENDCG
        }
    }
    Fallback Off
}
