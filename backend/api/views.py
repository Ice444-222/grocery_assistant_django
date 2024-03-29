from datetime import datetime

from django.db.models import BooleanField, Case, F, Sum, Value, When
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from recipes.models import Ingredient, Recipe, RecipesIngredients, Tag
from users.models import Subscription, User

from .filters import IngredientFilter, RecipeFilter
from .pagination import UserPageNumberPagination
from .permissions import IsAdmin, IsAdminOrReadOnly, SafeMethodOrAuthor
from .serializers import (IngredientSerializer, RecipeBriefSerializer,
                          RecipesSerializer, TagSerializer,
                          UserBasicSerializer, UserCreateSerializer,
                          UserNewPasswordSerializer,
                          UserSubscriptionsSerializer)


class UserViewSet(viewsets.ModelViewSet):
    """
    Вьюсет для модели User.
    """

    permission_classes = (permissions.AllowAny,)
    queryset = User.objects.all()
    http_method_names = ['get', 'post', 'put', 'delete']
    pagination_class = UserPageNumberPagination

    def get_serializer_class(self):
        """
        Функция вызывает сериализатор на основне вида метода.
        """

        if self.action == 'create':
            return UserCreateSerializer
        return UserBasicSerializer

    def perform_create(self, serializer):
        user = serializer.save()
        user.save()

    def create(self, request, *args, **kwargs):
        username = request.data.get("username")
        email = request.data.get("email")
        try:
            existing_user = User.objects.get(username=username, email=email)
            existing_user.save()
            response_data = {
                "email": email,
                "username": username
            }
            return Response(response_data, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            pass

        return super().create(request, *args, **kwargs)

    @action(
        detail=False, methods=['GET'],
        permission_classes=(IsAuthenticated,), url_path='me'
    )
    def me(self, request, *args, **kwargs):
        """
        Эндпоинт me для модели User. Показывает текущего пользователя.
        """

        user = request.user
        serializer = UserBasicSerializer(
            user,
            data=request.data, partial=True, context={'request': request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=False, methods=['POST'],
        permission_classes=(IsAuthenticated,)
    )
    def set_password(self, request, *args, **kwargs):
        """
        Эндпоинт set_password для нового пароля пользователя.
        """

        serializer = UserNewPasswordSerializer(
            data=request.data, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True, methods=['PUT'],
        permission_classes=(IsAdmin,)
    )
    def edit_user(self, request, pk=None):
        """
        Эндпоинт edit_user для редактирования пользователя.
        """

        user = self.get_object()
        serializer = UserBasicSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True, methods=['DELETE'],
        permission_classes=(IsAdmin,)
    )
    def delete_user(self, request, pk=None):
        """
        Эндпоинт delete_user для удаления пользователя.
        """

        user = self.get_object()
        user.delete()
        return Response(
            {"detail": "Пользователь удалён."},
            status=status.HTTP_204_NO_CONTENT
        )

    @action(
        detail=True, methods=['POST'],
        permission_classes=(IsAdmin,)
    )
    def block_user(self, request, pk=None):
        """
        Эндпоинт block_user для блокировки пользователя.
        """

        user = self.get_object()
        user.is_active = False
        user.save()
        return Response(
            {"detail": "Пользователь заблокирован."}, status=status.HTTP_200_OK
        )

    @action(
        detail=False, methods=['GET'],
        permission_classes=(IsAuthenticated,)
    )
    def subscriptions(self, request, *args, **kwargs):
        """
        Эндпоинт subscriptions для просмотра подписок текущего пользователя.
        """

        user = request.user
        user_subscriptions = User.objects.filter(subscribers__user=user)
        paginator = self.pagination_class()
        user_subscriptions_paginated = paginator.paginate_queryset(
            user_subscriptions, request
        )
        serializer = UserSubscriptionsSerializer(
            user_subscriptions_paginated, context={'request': request},
            many=True
        )
        return paginator.get_paginated_response(serializer.data)


class SubscribeUserAPIView(APIView):
    """
    Вью для создания и удаления подписок текущего пользователя.
    """

    permission_classes = (IsAuthenticated,)

    def post(self, request, pk=None):
        user = request.user
        subscription = get_object_or_404(User, pk=pk)
        user_subscriptions = Subscription.objects.filter(
            user=user, subscription=subscription
        )
        if user == subscription:
            return Response(
                {"detail": 'Вы не можете подписаться на самого себя.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if user_subscriptions.exists():
            return Response(
                {"detail": 'Вы уже подписаны на данного пользователя'},
                status=status.HTTP_400_BAD_REQUEST
            )
        Subscription.objects.create(
            user=request.user, subscription=subscription
        )
        serializer = UserSubscriptionsSerializer(
            subscription, context={'request': request},
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def delete(self, request, pk=None):
        user = request.user
        subscription = get_object_or_404(User, pk=pk)
        user_subscriptions = Subscription.objects.filter(
            user=user, subscription=subscription
        )
        if user_subscriptions.exists():
            user_subscriptions.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(
            {"detail": 'Вы не подписаны на этого пользователя'},
            status=status.HTTP_400_BAD_REQUEST
        )


class TokenLogoutView(APIView):
    """
    Вью для логаута и последующего удаления токена текущего пользователя.
    """

    permission_classes = (IsAuthenticated,)

    def post(self, request, *args, **kwargs):
        try:
            access_token = request.auth
            refresh_token = request.data.get('refresh_token')
            if access_token:
                OutstandingToken.objects.filter(token=access_token).delete()
            if refresh_token:
                RefreshToken(refresh_token).blacklist()
        except Exception:
            return Response(
                {'detail': 'Учетные данные не были предоставлены.'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class TagViewSet(viewsets.ModelViewSet):
    """
    Вьюсет для модели Tag.
    """

    permission_classes = (IsAdminOrReadOnly,)
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    pagination_class = None


class RecipeViewSet(viewsets.ModelViewSet):
    """
    Вьюсет для модели Recipe.
    """

    permission_classes = (SafeMethodOrAuthor | IsAdminOrReadOnly,)
    serializer_class = RecipesSerializer
    pagination_class = UserPageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_class = RecipeFilter

    def get_queryset(self):
        """
        Дополнительно аннотируем к queryset поля is_favorited и
        is_in_shopping_cart, которые нужны будут для других методов и функций.
        Также фэтчим и селектим tags, ingredients, author для оптимизации
        SQL запросов к базе данных.
        """
        user = self.request.user
        queryset = Recipe.objects.annotate(
            is_favorited=Case(
                When(favorites__pk=user.pk, then=Value(True)),
                default=Value(False),
                output_field=BooleanField()
            ),
            is_in_shopping_cart=Case(
                When(groceries_list__pk=user.pk, then=Value(True)),
                default=Value(False),
                output_field=BooleanField()
            )
        ).prefetch_related('tags', 'ingredients').select_related('author')
        return queryset

    def cart_favorite_method(self, pk, table):
        """
        Функция для сокращения однотипного кода для эндпоинта shopping_cart
        и favorite.
        """
        
        try:
            recipe = Recipe.objects.get(pk=pk)
        except Http404:
            raise ValidationError
        relation = table.filter(pk=recipe.pk)
        if relation.exists():
            return Response(
                status=status.HTTP_400_BAD_REQUEST
            )
        table.add(recipe)
        serializer = RecipeBriefSerializer(recipe)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def cart_favorite_method_delete(self, pk, table):
        """
        Функция для сокращения однотипного кода для эндпоинта shopping_cart
        и favorite где применён метод DELETE.
        """

        recipe = get_object_or_404(Recipe, pk=pk)
        relation = table.filter(pk=recipe.pk)
        if relation.exists():
            table.remove(recipe)
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(
            status=status.HTTP_400_BAD_REQUEST
        )

    @action(
        detail=True, methods=['POST'],
        permission_classes=(IsAuthenticated,)
    )
    def shopping_cart(self, request, *args, **kwargs):
        """
        Эндпоинт shopping_cart, представляет собой корзину пользователя,
        куда он может добавлять рецепты.
        """

        user = request.user
        pk = kwargs.get('pk')
        return self.cart_favorite_method(pk, user.groceries_list)

    @shopping_cart.mapping.delete
    def delete_shopping_cart(self, request, *args, **kwargs):
        """
        Метод DELETE для эндпоинта shopping_cart.
        """

        user = request.user
        pk = kwargs.get('pk')
        return self.cart_favorite_method_delete(pk, user.groceries_list)

    @action(
        detail=True, methods=['POST'],
        permission_classes=(IsAuthenticated,)
    )
    def favorite(self, request, *args, **kwargs):
        """
        Эндпоинт favorite, представляет собой список избранного пользователя,
        куда он может добавлять рецепты.
        """

        user = request.user
        pk = kwargs.get('pk')
        return self.cart_favorite_method(pk, user.favorites)

    @favorite.mapping.delete
    def delete_favorite(self, request, *args, **kwargs):
        """
        Метод DELETE для эндпоинта favorite.
        """

        user = request.user
        pk = kwargs.get('pk')
        return self.cart_favorite_method_delete(pk, user.favorites)

    @action(
        detail=False, methods=['GET'],
        permission_classes=(IsAuthenticated,)
    )
    def download_shopping_cart(self, request, *args, **kwargs):
        """
        Эндпоинт download_shopping_cart, позволяет скачивать список
        ингредиентов для покупки в формате .txt на основе
        рецептов в корзине пользователя.
        """

        user = request.user
        current_date = datetime.now().strftime("%Y-%m-%d")
        user_ingredients = (
            RecipesIngredients.objects
            .filter(recipe__groceries_list=user)
            .values(
                ingredient_name=F('ingredient__name'),
                measurement_unit=F('ingredient__measurement_unit')
            )
            .annotate(
                total_amount=Coalesce(Sum('amount'), Value(0)),
            )
        )
        content = (
            f"{user.username}, Ваш список покупок на {current_date}\n\n\n"
        )
        for ingredient in user_ingredients:
            content += (
                f"{ingredient['ingredient_name']}"
                f"({ingredient['measurement_unit']}) — "
                f"{ingredient['total_amount']}\n"
            )
        content += (
            '\n\n\nСформировано на сайте '
            'www.iceadmin.ru, проект Foodgram'
        )
        response = HttpResponse(content, content_type='text/plain')
        response['Content-Disposition'] = (
            'attachment; filename="shopping_list.txt"'
        )
        return response


class IngredientViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Вьюсет для ингредиентов.
    """

    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = (IsAdminOrReadOnly,)
    pagination_class = None
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_class = IngredientFilter
